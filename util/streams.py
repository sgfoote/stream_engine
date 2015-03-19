from Queue import Queue
from collections import OrderedDict
import importlib
import json
from threading import Event
import numexpr
import numpy
import struct
from scipy.interpolate import griddata
from werkzeug.exceptions import abort
from engine import app
import util.calc
import util.cass
from util.common import DataUnavailableException, UnknownEncodingException, FUNCTION, StreamNotFoundException, StreamKey, \
    TimeRange, parse_pdid, CachedParameter, CachedStream, UnknownFunctionTypeException

stream_cache = {}
parameter_cache = {}
function_cache = {}


class DataStream(object):
    def __init__(self, stream_key, time_range):
        self.stream_key = stream_key
        self.query_time_range = time_range
        self.available_time_range = TimeRange(0, 0)
        self.future = None
        self.row_cache = []
        self.queue = Queue()
        self.finished_event = Event()
        self.error = None
        self.data_cache = {}
        self.id_map = {}
        self.param_map = {}
        self.func_params = []
        self.times = []
        self._initialize()

    def _initialize(self):
        needs = set()
        for param in self.stream_key.stream.parameters:
            if not param.parameter_type == FUNCTION:
                self.id_map[param.id] = param.name

    def async_query(self):
        self.future = util.cass.fetch_data(self.stream_key, self.query_time_range)
        self.future.add_callbacks(callback=self.handle_page, errback=self.handle_error)

    def handle_page(self, rows):
        self.queue.put(rows)
        if self.future.has_more_pages:
            self.future.start_fetching_next_page()
        else:
            self.finished_event.set()

    def handle_error(self, exc):
        self.error = exc
        self.finished_event.set()

    def _get_chunk(self):
        chunk = self.queue.get()
        if hasattr(chunk, '_asdict'):
            self.row_cache.append(chunk)
        else:
            self.row_cache.extend(chunk)

        self.available_time_range.start = self.row_cache[0].time
        self.available_time_range.stop = self.row_cache[-1].time

    def create_generator(self, parameters):
        """
        generator to return the data from a single chunk
        dropping the previous row cache each cycle
        this is the preferred method of retrieving data
        from the primary stream
        """
        if parameters is None or len(parameters) == 0:
            parameters = [p for p in self.stream_key.stream.parameters if p.parameter_type != FUNCTION]

        while True:
            if self.queue.empty() and self.finished_event.is_set():
                raise StopIteration()

            self.row_cache = []
            self.data_cache = {p.id: [] for p in parameters}
            self._get_chunk()
            self.data_cache[7] = []

            if len(self.row_cache) == 0:
                raise StopIteration()

            fields = self.row_cache[0]._fields
            array = numpy.array(self.row_cache)

            for p in parameters:
                index = fields.index(p.name.lower())
                slice = array[:, index]
                shape_name = p.name + '_shape'
                if shape_name in fields:
                    shape = array[0, fields.index(shape_name)]
                    slice = self._handle_byte_buffer(''.join(slice), p.value_encoding, shape)
                slice = numpy.array(slice.tolist())
                self.data_cache[p.id] = slice

            yield self.data_cache

    def get_param(self, pdid, time_range):
        if pdid not in self.id_map:
            raise DataUnavailableException()

        if not all([self.queue.empty(), self.finished_event.is_set()]):
            while time_range.stop >= self.available_time_range.stop:
                if self.queue.empty() and self.finished_event.is_set():
                    break
                # grabbing new data, invalidate the old cache
                self.data_cache = {}
                self._get_chunk()

        if pdid not in self.data_cache:
            self._fill_cache(pdid)

        # copy the data in case of interpolation
        return self.data_cache[7][:], self.data_cache[pdid][:]

    def _fill_cache(self, pdid):
        name = self.id_map[pdid]
        if 7 not in self.data_cache:
            self.data_cache[7] = []
            for row in self.row_cache:
                self.data_cache[7].append(row.time)

        self.data_cache[pdid] = []
        for row in self.row_cache:
            item = getattr(row, name)
            if hasattr(row, name + '_shape'):
                shape = getattr(row, name + '_shape')
                item = self._handle_byte_buffer(item, self.param_map[name].value_encoding, shape)
            self.data_cache[pdid].append(item)

    def get_param_interp(self, pdid, interp_times):
        times, data = self.get_param(pdid, TimeRange(interp_times[0], interp_times[-1]))
        times, data = self._stretch(times, data, interp_times)
        times, data = self._interpolate(times, data, interp_times)
        return times, data

    @staticmethod
    def _stretch(times, data, interp_times):
        if len(times) == 1:
            return interp_times, data * len(interp_times)
        if interp_times[0] < times[0]:
            times.insert(0, interp_times[0])
            data.insert(0, data[0])
        if interp_times[-1] > times[-1]:
            times.append(interp_times[-1])
            data.append(data[-1])
        return times, data

    @staticmethod
    def _interpolate(times, data, interp_times):
        data = numpy.array(data)

        if numpy.array_equal(times, interp_times):
            return times, data
        try:
            data = data.astype('f64')
            data = griddata(times, data, interp_times, method='linear')
        except ValueError:
            data = DataStream._last_seen(times, data, interp_times)
        return interp_times, data

    @staticmethod
    def _last_seen(times, data, interp_times):
        time_index = 0
        last = data[0]
        next_time = times[1]
        new_data = []
        for t in interp_times:
            while t >= next_time:
                time_index += 1
                if time_index+1 < len(times):
                    next_time = times[time_index+1]
                    last = data[time_index]
                else:
                    last = data[time_index]
                    break
            new_data.append(last)
        return numpy.array(new_data)

    @staticmethod
    def _handle_byte_buffer(data, encoding, shape):
        if encoding in ['int8', 'int16', 'int32', 'uint8', 'uint16']:
            format_string = 'i'
            count = len(data) / 4
        elif encoding in ['uint32', 'int64']:
            format_string = 'l'
            count = len(data) / 8
        elif 'float' in encoding:
            format_string = 'd'
            count = len(data) / 8
        else:
            raise UnknownEncodingException()

        data = numpy.array(struct.unpack('>%d%s' % (count, format_string), data))
        data = data.reshape(shape)
        return data.tolist()


class StreamRequest2(object):
    def __init__(self, stream_keys, parameters, coefficients, time_range):
        self.stream_keys = stream_keys
        self.time_range = time_range
        self.parameters = parameters
        self.coefficients = coefficients
        self.streams = []
        self._initialize()

    def _initialize(self):
        if len(self.stream_keys) == 0:
            abort(400)

        # no duplicates allowed
        handled = []
        for key in self.stream_keys:
            if key in handled:
                abort(400)
            self.streams.append(self._create_data_stream(key))
            handled.append(key)

        # populate self.parameters if empty or None
        if self.parameters is None or len(self.parameters) == 0:
            self.parameters = set()
            for each in self.stream_keys:
                self.parameters = self.parameters.union(each.stream.parameters)

        # sort parameters by name for particle output
        params = [(p.name, p) for p in self.parameters]
        params.sort()
        self.parameters = [p[1] for p in params]

        # determine if any other parameters are needed
        distinct_sensors = util.cass.get_distinct_sensors()
        needs = set()
        for each in self.parameters:
            if each.parameter_type == FUNCTION:
                needs = needs.union([p for p in each.needs if p not in self.parameters])

        # available in the specified streams?
        provided = []
        for stream_key in self.stream_keys:
            provided.extend([p.id for p in stream_key.stream.parameters])

        needs = needs.difference(provided)

        # find the available streams which provide any needed parameters
        found = set()
        for each in needs:
            each = CachedParameter.from_id(each)
            if each in found:
                continue
            streams = [CachedStream.from_id(sid) for sid in each.streams]
            sensor1, stream1 = util.calc.find_stream(self.stream_keys[0], streams, distinct_sensors)
            if not any([sensor1 is None, stream1 is None]):
                new_stream_key = StreamKey.from_stream_key(self.stream_keys[0], sensor1, stream1.name)
                self.stream_keys.append(new_stream_key)
                self.streams.append(self._create_data_stream(new_stream_key))
                found = found.union(stream1.parameters)
        found = [p.id for p in found]

        if len(needs.difference(found)) > 0:
            app.logger.error('Unable to find needed parameters: %s', needs.difference(found))
            abort(404)

    def _create_data_stream(self, stream_key):
        if stream_key.stream is None:
            raise StreamNotFoundException(stream_key.stream_name)

        return DataStream(stream_key, self.time_range)

    def _query_all(self):
        for stream in self.streams:
            stream.async_query()

    def _calculate(self, parameter, chunk):
        needs = [CachedParameter.from_id(p) for p in parameter.needs if p not in chunk.keys()]
        if parameter in needs:
            needs.remove(parameter)
        for each in needs:
            # this should descend through any L2 functions to
            # calculate the underlying L1 functions first
            if each.parameter_type == FUNCTION:
                self._calculate(each, chunk)
            for stream in self.streams[1:]:
                try:
                    # we may have already inserted this during recursion
                    if each.id not in chunk:
                        times, data = stream.get_param_interp(each.id, chunk[7])
                        chunk[each.id] = data
                except DataUnavailableException:
                    pass

        args = self.build_func_map(parameter, chunk)
        chunk[parameter.id] = self._execute_dpa(parameter, args)

    def _execute_dpa(self, parameter, kwargs):
        func = parameter.parameter_function
        func_map = parameter.parameter_function_map

        if len(kwargs) == len(func_map):
            if func.function_type == 'PythonFunction':
                module = importlib.import_module(func.owner)
                result = getattr(module, func.function)(**kwargs)
            elif func.function_type == 'NumexprFunction':
                result = numexpr.evaluate(func.function, kwargs)
            else:
                raise UnknownFunctionTypeException(func.function_type)
            return result

    def build_func_map(self, parameter, chunk):

        func_map = parameter.parameter_function_map
        args = {}
        for key in func_map:
            if func_map[key].startswith('PD'):
                pdid = parse_pdid(func_map[key])

                if pdid not in chunk:
                    raise DataUnavailableException(pdid)

                args[key] = chunk[pdid]

            elif func_map[key].startswith('CC'):
                name = func_map[key]
                if name in self.coefficients:
                    args[key] = self.coefficients[name]
                else:
                    args[key] = 1.0
                    #raise CoefficientUnavailableException(name)
        return args

    def chunk_to_particles(self, chunk):
        pk = self.stream_keys[0].as_dict()

        for index, t in enumerate(chunk[7]):
            particle = OrderedDict()
            particle['pk'] = pk
            pk['time'] = t
            for param in self.parameters:
                particle[param.name] = chunk[param.id][index]
            yield json.dumps(particle)

    def particle_generator(self):
        # plan of attack
        # start queries
        # fetch chunk from primary stream
        # retrieve times for chunk
        # fetch chunk from each secondary stream until time parity reached or chunks exhausted
        # retrieve raw data from primary stream, interpolated data from secondary streams
        # calculate derived products
        # yield one or more particles
        self._query_all()
        yield '[\n'
        first = True
        for chunk in self.streams[0].create_generator(None):
            for parameter in self.parameters:
                if parameter.id not in chunk:
                    if parameter.parameter_type == FUNCTION:
                        self._calculate(parameter, chunk)
                    else:
                        for stream in self.streams[1:]:
                            try:
                                chunk[parameter.id] = stream.get_param_interp(parameter.id, chunk[7])[1]
                            except DataUnavailableException:
                                pass
            for particle in self.chunk_to_particles(chunk):
                if first:
                    first = False
                else:
                    yield ',\n'
                yield particle
        yield '\n]\n'
