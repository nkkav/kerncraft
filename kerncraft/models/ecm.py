#!/usr/bin/env python
from __future__ import print_function
from __future__ import unicode_literals
from __future__ import absolute_import
from __future__ import division

import copy
import sys
import subprocess
import re
import math
from pprint import pprint, pformat
from distutils.spawn import find_executable

import six
try:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plot_support = True
except ImportError:
    plot_support = False

from kerncraft.prefixedunit import PrefixedUnit
from kerncraft.kernel import KernelCode


def round_to_next(x, base):
    # Based on: http://stackoverflow.com/a/2272174
    return int(base * math.ceil(float(x)/base))


def blocking(indices, block_size, initial_boundary=0):
    '''
    splits list of integers into blocks of block_size. returns block indices.

    first block element is located at initial_boundary (default 0).

    >>> blocking([0, -1, -2, -3, -4, -5, -6, -7, -8, -9], 8)
    [0,-1]
    >>> blocking([0], 8)
    [0]
    '''
    blocks = []

    for idx in indices:
        bl_idx = (idx-initial_boundary)//float(block_size)
        if bl_idx not in blocks:
            blocks.append(bl_idx)
    blocks.sort()

    return blocks


class ECMData(object):
    """
    class representation of the Execution-Cache-Memory Model (only the data part)

    more info to follow...
    """

    name = "Execution-Cache-Memory (data transfers only)"

    @classmethod
    def configure_arggroup(cls, parser):
        pass

    def __init__(self, kernel, machine, args=None, parser=None):
        """
        *kernel* is a Kernel object
        *machine* describes the machine (cpu, cache and memory) characteristics
        *args* (optional) are the parsed arguments from the comand line
        """
        self.kernel = kernel
        self.machine = machine
        self._args = args
        self._parser = parser

        if args:
            # handle CLI info
            pass

    def calculate_cache_access(self):
        # FIXME handle multiple datatypes
        element_size = self.kernel.datatypes_size[self.kernel.datatype]
        cacheline_size = self.machine['cacheline size']
        elements_per_cacheline = int(cacheline_size // element_size)

        # Get the machine's cache model and simulator
        csim = self.machine.get_cachesim()

        # Calculate the number of iterations necessary for warm-up
        max_cache_size = max(map(lambda c: c.size(), csim.levels(with_mem=False)))
        max_array_size = max(self.kernel.array_sizes(in_bytes=True, subs_consts=True).values())

        offsets = []
        if max_array_size < max_cache_size:
            # Full caching possible, go through all itreration before actual initialization
            warmup_iteration_count = self.kernel.iteration_length()//3
            offsets = list(self.kernel.compile_global_offsets(
                iteration=range(0, self.kernel.iteration_length())))

        # Regular Initialization
        warmup_iteration_count = min(2*self.kernel.iteration_length()//3,
                                     2*max_cache_size//element_size//3)

        # Make sure warmup_iteration_count is not near a boundary (otherwise jumps might give worse
        # cache results)
        # TODO can we find a nicer solution?
        first_dim_length = self.kernel.iteration_length(-1)
        if warmup_iteration_count % first_dim_length < 0.1*first_dim_length:
            # to close to the beginning, increase 20%
            warmup_iteration_count += int(0.2*first_dim_length)
        if abs(warmup_iteration_count % first_dim_length - first_dim_length) < 0.1*first_dim_length:
            # to close to the end, subtract 20%
            warmup_iteration_count -= int(0.2*first_dim_length)
        
        # Align iteration count with cachelines
        # do this by aligning either writes (preferred) or reads:
        # Assumption: writes (and reads) increase linearly
        o = list(self.kernel.compile_global_offsets(iteration=warmup_iteration_count))[0]
        if o[1]:
            # we have a write to work with:
            first_offset = min(o[1])
        else:
            # we use reads
            first_offset = min(o[0])
        # Distance from cacheline boundary (in bytes)
        diff = first_offset - \
               (int(first_offset)>>csim.first_level.cl_bits<<csim.first_level.cl_bits)

        warmup_iteration_count -= diff//element_size

        offsets += list(self.kernel.compile_global_offsets(
            iteration=range(0, warmup_iteration_count)))

        # Do the warm-up
        csim.loadstore(offsets, length=element_size)
        # FIXME compile_global_offsets should already expand to element_size

        # Force write-back on all cache levels
        csim.force_write_back()

        # Reset stats to conclude warm-up phase
        csim.reset_stats()

        # Benchmark iterations:
        bench_iteration_start = warmup_iteration_count
        bench_iteration_end = bench_iteration_start+elements_per_cacheline

        # compile access needed for one cache-line
        offsets = self.kernel.compile_global_offsets(
            iteration=range(bench_iteration_start, bench_iteration_end))
        offsets = list(offsets)

        # simulate
        csim.loadstore(offsets, length=element_size)
        # FIXME compile_global_offsets should already expand to element_size

        # Force write-back on all cache levels
        csim.force_write_back()

        # use stats to build results
        stats = list(csim.stats())

        # Check for layer condition towards all cache levels (except main memory/last level)
        self.results = {'memory hierarchy': [], 'cycles': [], 'cache stats': stats}
        for cache_level, cache_info in list(enumerate(self.machine['memory hierarchy']))[:-1]:
            self.results['memory hierarchy'].append({
                'index': len(self.results['memory hierarchy']),
                'level': '{}'.format(cache_info['level']),
                'total misses': stats[cache_level]['MISS_byte'],
                'total hits': stats[cache_level]['HIT_byte'],
                'total evicts': stats[cache_level]['STORE_byte'],
                'total lines misses': stats[cache_level]['MISS_count'],
                'total lines hits': stats[cache_level]['HIT_count'],
                # FIXME assumption for line evicts: all stores are consecutive
                'total lines evicts': stats[cache_level+1]['STORE_count'],
                'cycles': None})

    def calculate_cycles(self):
        element_size = self.kernel.datatypes_size[self.kernel.datatype]
        elements_per_cacheline = float(self.machine['cacheline size']) // element_size

        for cache_level, cache_info in list(enumerate(self.machine['memory hierarchy']))[:-1]:
            cache_cycles = cache_info['cycles per cacheline transfer']
            cache_results = self.results['memory hierarchy'][cache_level]

            if cache_cycles is not None:
                # only cache cycles count
                cycles = (cache_results['total lines misses']
                          + cache_results['total lines evicts']) * \
                         cache_cycles
            else:
                # Memory transfer
                # we use bandwidth to calculate cycles and then add panalty cycles (if given)

                # choose bw according to cache level and problem
                # first, compile stream counts at current cache level
                # write-allocate is allready resolved above
                read_streams = cache_results['total lines misses']
                write_streams = cache_results['total lines evicts']
                # second, try to find best fitting kernel (closest to stream seen stream counts):
                threads_per_core = 1
                bw, measurement_kernel = self.machine.get_bandwidth(
                    cache_level+1, read_streams, write_streams, threads_per_core)

                # calculate cycles
                cycles = float(cache_results['total lines misses'] +
                               cache_results['total lines evicts']) *\
                    float(elements_per_cacheline) * float(element_size) * \
                    float(self.machine['clock']) / float(bw)
                # add penalty cycles for each read stream
                if 'penalty cycles per read stream' in cache_info:
                    cycles += cache_results['total lines misses'] * \
                              cache_info['penalty cycles per read stream']

            cache_results['cycles'] =  cycles

            if cache_cycles is None:
                cache_results.update({
                    'memory bandwidth kernel': measurement_kernel,
                    'memory bandwidth': bw})

            self.results['cycles'].append((
                '{}-{}'.format(
                    cache_info['level'], self.machine['memory hierarchy'][cache_level+1]['level']),
                cycles))

            # TODO remove the following by makeing testcases more versatile:
            self.results['{}-{}'.format(
                cache_info['level'], self.machine['memory hierarchy'][cache_level+1]['level'])
                ] = cycles

        return self.results

    def analyze(self):
        self.calculate_cache_access()
        self.calculate_cycles()

        return self.results

    def conv_cy(self, cy_cl, unit, default='cy/CL'):
        '''Convert cycles (cy/CL) to other units, such as FLOP/s or It/s'''
        if not isinstance(cy_cl, PrefixedUnit):
            cy_cl = PrefixedUnit(cy_cl, '', 'cy/CL')
        if not unit:
            unit = default

        clock = self.machine['clock']
        element_size = self.kernel.datatypes_size[self.kernel.datatype]
        elements_per_cacheline = int(self.machine['cacheline size']) // element_size
        it_s = clock/cy_cl*elements_per_cacheline
        it_s.unit = 'It/s'
        flops_per_it = sum(self.kernel._flops.values())
        performance = it_s*flops_per_it
        performance.unit = 'FLOP/s'
        cy_it = cy_cl*elements_per_cacheline
        cy_it.unit = 'cy/It'

        return {'It/s': it_s,
                'cy/CL': cy_cl,
                'cy/It': cy_it,
                'FLOP/s': performance}[unit]

    def report(self, output_file=sys.stdout):
        if self._args and self._args.verbose > 1:
            print('Cache simulation statistics:', file=output_file)
            print('{!r}'.format(self.results['cache stats']), file=output_file)
            for cs in self.results['cache stats']:
                print('{:>5} {!r}'.format(cs['name'], {k:v for k,v in cs.items() if v != 'name'}),
                      file=output_file)

        for level, cycles in self.results['cycles']:
            print('{} = {}'.format(
                level, self.conv_cy(float(cycles), self._args.unit)), file=output_file)


class ECMCPU(object):
    """
    class representation of the Execution-Cache-Memory Model (only the operation part)

    more info to follow...
    """

    name = "Execution-Cache-Memory (CPU operations only)"

    @classmethod
    def configure_arggroup(cls, parser):
        pass

    def __init__(self, kernel, machine, args=None, parser=None):
        """
        *kernel* is a Kernel object
        *machine* describes the machine (cpu, cache and memory) characteristics
        *args* (optional) are the parsed arguments from the comand line
        if *args* is given also *parser* has to be provided
        """
        if not isinstance(kernel, KernelCode):
            raise ValueError("Kernel was not derived from code, can not perform ECMCPU analysis."
                             "Try ECMData.")
        self.kernel = kernel
        self.machine = machine
        self._args = args
        self._parser = parser

        if args:
            # handle CLI info
            if self._args.asm_block not in ['auto', 'manual']:
                try:
                    self._args.asm_block = int(args.asm_block)
                except ValueError:
                    parser.error('--asm-block can only be "auto", "manual" or an integer')

    def analyze(self):
        # For the IACA/CPU analysis we need to compile and assemble
        asm_name = self.kernel.compile(
            self.machine['compiler'], compiler_args=self.machine['compiler flags'])
        bin_name = self.kernel.assemble(
            self.machine['compiler'], asm_name, iaca_markers=True, asm_block=self._args.asm_block,
            asm_increment=self._args.asm_increment)

        # Making sure iaca.sh is available:
        if find_executable('iaca.sh') is None:
            print("iaca.sh was not found. Make sure it is found in PATH.", file=sys.stderr)
            sys.exit(1)

        try:
            cmd = ['iaca.sh', '-64', '-arch', self.machine['micro-architecture'], bin_name]
            iaca_output = subprocess.check_output(cmd).decode('utf-8')
        except OSError as e:
            print("IACA execution failed:", ' '.join(cmd), file=sys.stderr)
            print(e, file=sys.stderr)
            sys.exit(1)
        except subprocess.CalledProcessError as e:
            print("IACA throughput analysis failed:", e, file=sys.stderr)
            sys.exit(1)

        # Get total cycles per loop iteration
        match = re.search(
            r'^Block Throughput: ([0-9\.]+) Cycles', iaca_output, re.MULTILINE)
        assert match, "Could not find Block Throughput in IACA output."
        block_throughput = float(match.groups()[0])

        # Find ports and cyles per port
        ports = [l for l in iaca_output.split('\n') if l.startswith('|  Port  |')]
        cycles = [l for l in iaca_output.split('\n') if l.startswith('| Cycles |')]
        assert ports and cycles, "Could not find ports/cylces lines in IACA output."
        ports = [p.strip() for p in ports[0].split('|')][2:]
        cycles = [c.strip() for c in cycles[0].split('|')][2:]
        port_cycles = []
        for i in range(len(ports)):
            if '-' in ports[i] and ' ' in cycles[i]:
                subports = [p.strip() for p in ports[i].split('-')]
                subcycles = [c for c in cycles[i].split(' ') if bool(c)]
                port_cycles.append((subports[0], float(subcycles[0])))
                port_cycles.append((subports[0]+subports[1], float(subcycles[1])))
            elif ports[i] and cycles[i]:
                port_cycles.append((ports[i], float(cycles[i])))
        port_cycles = dict(port_cycles)

        match = re.search(r'^Total Num Of Uops: ([0-9]+)', iaca_output, re.MULTILINE)
        assert match, "Could not find Uops in IACA output."
        uops = float(match.groups()[0])

        # Get latency prediction from IACA
        try:
            iaca_latency_output = subprocess.check_output(
                ['iaca.sh', '-64', '-analysis', 'LATENCY', '-arch',
                 self.machine['micro-architecture'], bin_name]).decode('utf-8')
        except subprocess.CalledProcessError as e:
            print("IACA latency analysis failed:", e, file=sys.stderr)
            sys.exit(1)
        match = re.search(
            r'^Latency: ([0-9\.]+) Cycles', iaca_latency_output, re.MULTILINE)
        assert match, "Could not find Latency in IACA latency analysis output."
        block_latency = float(match.groups()[0])

        # Normalize to cycles per cacheline
        elements_per_block = abs(self.kernel.asm_block['pointer_increment']
                                 // self.kernel.datatypes_size[self.kernel.datatype])
        block_size = elements_per_block*self.kernel.datatypes_size[self.kernel.datatype]
        try:
            block_to_cl_ratio = float(self.machine['cacheline size'])/block_size
        except ZeroDivisionError as e:
            print("Too small block_size / pointer_increment:", e, file=sys.stderr)
            sys.exit(1)

        port_cycles = dict([(i[0], i[1]*block_to_cl_ratio) for i in list(port_cycles.items())])
        uops = uops*block_to_cl_ratio
        cl_throughput = block_throughput*block_to_cl_ratio
        cl_latency = block_latency*block_to_cl_ratio

        # Compile most relevant information
        T_OL = max(
            [v for k, v in list(port_cycles.items()) if k in self.machine['overlapping ports']])
        T_nOL = max(
            [v for k, v in list(port_cycles.items()) if k in self.machine['non-overlapping ports']])

        # Use IACA throughput prediction if it is slower then T_nOL
        if T_nOL < cl_throughput:
            T_OL = cl_throughput

        # Use latency if requested
        if self._args.latency:
            T_OL = cl_latency

        # Create result dictionary
        self.results = {
            'port cycles': port_cycles,
            'cl throughput': cl_throughput,
            'cl latency': cl_latency,
            'uops': uops,
            'T_nOL': T_nOL,
            'T_OL': T_OL,
            'IACA output': iaca_output,
            'IACA latency output': iaca_latency_output}


    def conv_cy(self, cy_cl, unit, default='cy/CL'):
        '''Convert cycles (cy/CL) to other units, such as FLOP/s or It/s'''
        if not isinstance(cy_cl, PrefixedUnit):
            cy_cl = PrefixedUnit(cy_cl, '', 'cy/CL')
        if not unit:
            unit = default

        clock = self.machine['clock']
        element_size = self.kernel.datatypes_size[self.kernel.datatype]
        elements_per_cacheline = int(self.machine['cacheline size']) // element_size
        it_s = clock/cy_cl*elements_per_cacheline
        it_s.unit = 'It/s'
        flops_per_it = sum(self.kernel._flops.values())
        performance = it_s*flops_per_it
        performance.unit = 'FLOP/s'
        cy_it = cy_cl*elements_per_cacheline
        cy_it.unit = 'cy/It'

        return {'It/s': it_s,
                'cy/CL': cy_cl,
                'cy/It': cy_it,
                'FLOP/s': performance}[unit]

    def report(self, output_file=sys.stdout):
        if self._args and self._args.verbose > 2:
            print("IACA Output:", file=output_file)
            print(self.results['IACA output'], file=output_file)
            print(self.results['IACA latency output'], file=output_file)
            print('', file=output_file)

        if self._args and self._args.verbose > 1:
            print('Ports and cycles:', six.text_type(self.results['port cycles']), file=output_file)
            print('Uops:', six.text_type(self.results['uops']), file=output_file)

            print('Throughput: {}'.format(
                      self.conv_cy(self.results['cl throughput'], self._args.unit)),
                  file=output_file)

            print('Latency: {}'.format(
                      self.conv_cy(self.results['cl latency'], self._args.unit)),
                  file=output_file)

        print('T_nOL = {:.2g} cy/CL'.format(self.results['T_nOL']), file=output_file)
        print('T_OL = {:.2g} cy/CL'.format(self.results['T_OL']), file=output_file)


class ECM(object):
    """
    class representation of the Execution-Cache-Memory Model (data and operations)

    more info to follow...
    """

    name = "Execution-Cache-Memory"

    @classmethod
    def configure_arggroup(cls, parser):
        # they are being configured in ECMData and ECMCPU
        parser.add_argument(
            '--ecm-plot',
            help='Filename to save ECM plot to (supported extensions: pdf, png, svg and eps)')

    def __init__(self, kernel, machine, args=None, parser=None):
        """
        *kernel* is a Kernel object
        *machine* describes the machine (cpu, cache and memory) characteristics
        *args* (optional) are the parsed arguments from the comand line
        """
        if not isinstance(kernel, KernelCode):
            raise ValueError("Kernel was not derived from code, can not perform ECM analysis. "
                             "Try ECMData.")
        self.kernel = kernel
        self.machine = machine
        self._args = args

        if args:
            # handle CLI info
            pass

        self._CPU = ECMCPU(kernel, machine, args, parser)
        self._data = ECMData(kernel, machine, args, parser)

    def analyze(self):
        self._CPU.analyze()
        self._data.analyze()
        self.results = copy.deepcopy(self._CPU.results)
        self.results.update(copy.deepcopy(self._data.results))

        # Saturation/multi-core scaling analysis
        # very simple approach. Assumptions are:
        #  - bottleneck is always LLC-MEM
        #  - all caches scale with number of cores (bw AND size(WRONG!))
        if self.results['cycles'][-1][1] == 0.0:
            # Full caching in higher cache level
            self.results['scaling cores'] = float('inf')
        else:
            self.results['scaling cores'] = int(math.ceil(
                max(self.results['T_OL'],
                    self.results['T_nOL'] + sum([c[1] for c in self.results['cycles']])) /
                self.results['cycles'][-1][1]))

    def report(self, output_file=sys.stdout):
        report = ''
        if self._args and self._args.verbose > 1:
            self._CPU.report()
            self._data.report()

        total_cycles = max(
            self.results['T_OL'],
            sum([self.results['T_nOL']]+[i[1] for i in self.results['cycles']]))
        report += '{{ {:.2g} || {:.2g} | {} }} cy/CL'.format(
            self.results['T_OL'],
            self.results['T_nOL'],
            ' | '.join(['{:.2g}'.format(i[1]) for i in self.results['cycles']]))

        if self._args.unit:
            report += ' = {}'.format(self._CPU.conv_cy(total_cycles, self._args.unit))

        report += '\n{{ {} \ {} }} cy/CL'.format(
            max(self.results['T_OL'],
                self.results['T_nOL']),
                ' \ '.join(['{:.2g}'.format(max(sum([x[1] for x in self.results['cycles'][:i+1]]) +
                                                self.results['T_nOL'], self.results['T_OL']))
                            for i in range(len(self.results['cycles']))]))

        report += '\nsaturating at {} cores'.format(self.results['scaling cores'])

        print(report, file=output_file)

        if self._args and self._args.ecm_plot:
            assert plot_support, "matplotlib couldn't be imported. Plotting is not supported."

            fig = plt.figure(frameon=False)
            fig.subplots_adjust(left=0.1, right=0.9, top=0.9, bottom=0.15)
            ax = fig.add_subplot(1, 1, 1)

            sorted_overlapping_ports = sorted(
                [(p, self.results['port cycles'][p]) for p in self.machine['overlapping ports']],
                key=lambda x: x[1])

            yticks_labels = []
            yticks = []
            xticks_labels = []
            xticks = []

            # Plot configuration
            height = 0.9

            i = 0
            # T_OL
            colors = [(254./255, 177./255., 178./255.)] + [(255./255., 255./255., 255./255.)] * \
                (len(sorted_overlapping_ports) - 1)
            for p, c in sorted_overlapping_ports:
                ax.barh(i, c, height, align='center', color=colors.pop())
                if i == len(sorted_overlapping_ports)-1:
                    ax.text(c/2.0, i, '$T_\mathrm{OL}$', ha='center', va='center')
                yticks_labels.append(p)
                yticks.append(i)
                i += 1
            xticks.append(sorted_overlapping_ports[-1][1])
            xticks_labels.append('{:.2g}'.format(sorted_overlapping_ports[-1][1]))

            # T_nOL + memory transfers
            y = 0
            colors = [(187./255., 255/255., 188./255.)] * (len(self.results['cycles'])) + \
                [(119./255, 194./255., 255./255.)]
            for k, v in [('nOL', self.results['T_nOL'])]+self.results['cycles']:
                ax.barh(i, v, height, y, align='center', color=colors.pop())
                ax.text(y+v/2.0, i, '$T_\mathrm{'+k+'}$', ha='center', va='center')
                xticks.append(y+v)
                xticks_labels.append('{:.2g}'.format(y+v))
                y += v
            yticks_labels.append('LD')
            yticks.append(i)

            ax.tick_params(axis='y', which='both', left='off', right='off')
            ax.tick_params(axis='x', which='both', top='off')
            ax.set_xlabel('t [cy]')
            ax.set_ylabel('execution port')
            ax.set_yticks(yticks)
            ax.set_yticklabels(yticks_labels)
            ax.set_xticks(xticks)
            ax.set_xticklabels(xticks_labels, rotation='vertical')
            ax.xaxis.grid(alpha=0.7, linestyle='--')
            fig.savefig(self._args.ecm_plot)

