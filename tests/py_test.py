from scannerpy import (Database, Config, DeviceType, ColumnType, Job,
                       ProtobufGenerator, ScannerException, Kernel)
from scannerpy.stdlib import readers
import tempfile
import toml
import pytest
from subprocess import check_call as run
from multiprocessing import Process, Queue
import requests
import imp
import os.path
import socket
import numpy as np
import sys
import grpc
import struct
import pickle
import subprocess

try:
    run(['nvidia-smi'])
    has_gpu = True
except (OSError, subprocess.CalledProcessError) as e:
    has_gpu = False

gpu = pytest.mark.skipif(not has_gpu, reason='need GPU to run')
slow = pytest.mark.skipif(
    not pytest.config.getoption('--runslow'),
    reason='need --runslow option to run')

cwd = os.path.dirname(os.path.abspath(__file__))


@slow
def test_tutorial():
    def run_py(path):
        print(path)
        run('cd {}/../examples/tutorial && python3 {}.py'.format(cwd, path),
            shell=True)

    run('cd {}/../examples/tutorial/resize_op && '
        'mkdir -p build && cd build && cmake -D SCANNER_PATH={} .. && '
        'make'.format(cwd, cwd + '/..'),
        shell=True)

    tutorials = [
        '00_basic', '01_sampling', '02_collections', '03_ops',
        '04_compression', '05_custom_op'
    ]

    for t in tutorials:
        run_py(t)


@slow
def test_examples():
    def run_py(arg):
        [d, f] = arg
        print(f)
        run('cd {}/../examples/{} && python3 {}.py'.format(cwd, d, f),
            shell=True)

    examples = [['face_detection', 'face_detect'],
                ['shot_detection', 'shot_detect']]

    for e in examples:
        run_py(e)


def make_config(master_port=None, worker_port=None, path=None):
    cfg = Config.default_config()
    cfg['network']['master'] = 'localhost'
    cfg['storage']['db_path'] = tempfile.mkdtemp()
    if master_port is not None:
        cfg['network']['master_port'] = master_port
    if worker_port is not None:
        cfg['network']['worker_port'] = worker_port

    if path is not None:
        with open(path, 'w') as f:
            cfg_path = path
            f.write(toml.dumps(cfg))
    else:
        with tempfile.NamedTemporaryFile(delete=False) as f:
            cfg_path = f.name
            f.write(bytes(toml.dumps(cfg), 'utf-8'))
    return (cfg_path, cfg)


def download_videos():
    # Download video from GCS
    url = "https://storage.googleapis.com/scanner-data/test/short_video.mp4"
    with tempfile.NamedTemporaryFile(delete=False, suffix='.mp4') as f:
        host = socket.gethostname()
        # HACK: special proxy case for Ocean cluster
        if host in ['ocean', 'crissy', 'pismo', 'stinson']:
            resp = requests.get(
                url,
                stream=True,
                proxies={
                    'https': 'http://proxy.pdl.cmu.edu:3128/'
                })
        else:
            resp = requests.get(url, stream=True)
        assert resp.ok
        for block in resp.iter_content(1024):
            f.write(block)
        vid1_path = f.name

    # Make a second one shorter than the first
    with tempfile.NamedTemporaryFile(delete=False, suffix='.mp4') as f:
        vid2_path = f.name
    run([
        'ffmpeg', '-y', '-i', vid1_path, '-ss', '00:00:00', '-t', '00:00:10',
        '-c:v', 'libx264', '-strict', '-2', vid2_path
    ])

    return (vid1_path, vid2_path)


@pytest.fixture(scope="module")
def db():
    # Create new config
    (cfg_path, cfg) = make_config()

    # Setup and ingest video
    with Database(config_path=cfg_path, debug=True) as db:
        (vid1_path, vid2_path) = download_videos()

        db.ingest_videos([('test1', vid1_path), ('test2', vid2_path)])

        db.ingest_videos(
            [('test1_inplace', vid1_path), ('test2_inplace', vid2_path)],
            inplace=True)

        yield db

        # Tear down
        run([
            'rm', '-rf', cfg['storage']['db_path'], cfg_path, vid1_path,
            vid2_path
        ])


def test_new_database(db):
    pass


def test_table_properties(db):
    for name, i in [('test1', 0), ('test1_inplace', 2)]:
        table = db.table(name)
        assert table.id() == i
        assert table.name() == name
        assert table.num_rows() == 720
        assert [c for c in table.column_names()] == ['index', 'frame']


def test_summarize(db):
    db.summarize()


def test_load_video_column(db):
    for name in ['test1', 'test1_inplace']:
        next(db.table(name).load(['frame']))


def test_gather_video_column(db):
    for name in ['test1', 'test1_inplace']:
        # Gather rows
        rows = [0, 10, 100, 200]
        frames = [_ for _ in db.table(name).load(['frame'], rows=rows)]
        assert len(frames) == len(rows)


def test_profiler(db):
    frame = db.sources.FrameColumn()
    hist = db.ops.Histogram(frame=frame)
    output_op = db.sinks.Column(columns={'hist': hist})

    job = Job(op_args={
        frame: db.table('test1').column('frame'),
        output_op: '_ignore'
    })

    output = db.run(output_op, [job], show_progress=False, force=True)
    profiler = output[0].profiler()
    f = tempfile.NamedTemporaryFile(delete=False)
    f.close()
    profiler.write_trace(f.name)
    profiler.statistics()
    run(['rm', '-f', f.name])


def test_new_table(db):
    def b(s):
        return bytes(s, 'utf-8')

    db.new_table('test', ['col1', 'col2'],
                 [[b('r00'), b('r01')], [b('r10'), b('r11')]])
    t = db.table('test')
    assert (t.num_rows() == 2)
    assert (next(t.column('col2').load()) == b('r01'))


def test_sample(db):
    def run_sampler_job(sampler_args, expected_rows):
        frame = db.sources.FrameColumn()
        sample_frame = frame.sample()
        output_op = db.sinks.Column(columns={'frame': sample_frame})

        job = Job(
            op_args={
                frame: db.table('test1').column('frame'),
                sample_frame: sampler_args,
                output_op: 'test_sample',
            })

        tables = db.run(output_op, [job], force=True, show_progress=False)
        num_rows = 0
        for _ in tables[0].column('frame').load():
            num_rows += 1
        assert num_rows == expected_rows

    # Stride
    expected = int((db.table('test1').num_rows() + 8 - 1) / 8)
    run_sampler_job(db.sampler.strided(8), expected)
    # Range
    run_sampler_job(db.sampler.range(0, 30), 30)
    # Strided Range
    run_sampler_job(db.sampler.strided_range(0, 300, 10), 30)
    # Gather
    run_sampler_job(db.sampler.gather([0, 150, 377, 500]), 4)


def test_space(db):
    def run_spacer_job(spacing_args):
        frame = db.sources.FrameColumn()
        hist = db.ops.Histogram(frame=frame)
        space_hist = hist.space()
        output_op = db.sinks.Column(columns={'histogram': space_hist})

        job = Job(
            op_args={
                frame: db.table('test1').column('frame'),
                space_hist: spacing_args,
                output_op: 'test_space',
            })

        tables = db.run(output_op, [job], force=True, show_progress=False)
        return tables[0]

    # Repeat
    spacing_distance = 8
    table = run_spacer_job(db.sampler.space_repeat(spacing_distance))
    num_rows = 0
    for hist in table.column('histogram').load(readers.histograms):
        # Verify outputs are repeated correctly
        if num_rows % spacing_distance == 0:
            ref_hist = hist
        assert len(hist) == 3
        for c in range(len(hist)):
            assert (ref_hist[c] == hist[c]).all()
        num_rows += 1
    assert num_rows == db.table('test1').num_rows() * spacing_distance

    # Null
    table = run_spacer_job(db.sampler.space_null(spacing_distance))
    num_rows = 0
    for hist in table.column('histogram').load(readers.histograms):
        # Verify outputs are None for null rows
        if num_rows % spacing_distance == 0:
            assert hist is not None
            assert len(hist) == 3
            assert hist[0].shape[0] == 16
        else:
            assert hist is None
        num_rows += 1
    assert num_rows == db.table('test1').num_rows() * spacing_distance


def test_slice(db):
    frame = db.sources.FrameColumn()
    slice_frame = frame.slice()
    unsliced_frame = slice_frame.unslice()
    output_op = db.sinks.Column(columns={'frame': unsliced_frame})
    job = Job(
        op_args={
            frame: db.table('test1').column('frame'),
            slice_frame: db.partitioner.all(50),
            output_op: 'test_slicing',
        })

    tables = db.run(output_op, [job], force=True, show_progress=False)

    num_rows = 0
    for _ in tables[0].column('frame').load():
        num_rows += 1
    assert num_rows == db.table('test1').num_rows()


def test_overlapping_slice(db):
    frame = db.sources.FrameColumn()
    slice_frame = frame.slice()
    sample_frame = slice_frame.sample()
    unsliced_frame = sample_frame.unslice()
    output_op = db.sinks.Column(columns={'frame': unsliced_frame})
    job = Job(
        op_args={
            frame:
            db.table('test1').column('frame'),
            slice_frame:
            db.partitioner.strided_ranges([(0, 15), (5, 25), (15, 35)], 1),
            sample_frame: [
                db.sampler.range(0, 10),
                db.sampler.range(5, 15),
                db.sampler.range(5, 15),
            ],
            output_op:
            'test_slicing',
        })

    tables = db.run(output_op, [job], force=True, show_progress=False)

    num_rows = 0
    for _ in tables[0].column('frame').load():
        num_rows += 1
    assert num_rows == 30


def test_bounded_state(db):
    warmup = 3

    frame = db.sources.FrameColumn()
    increment = db.ops.TestIncrementBounded(ignore=frame, warmup=warmup)
    sampled_increment = increment.sample()
    output_op = db.sinks.Column(columns={'integer': sampled_increment})
    job = Job(
        op_args={
            frame: db.table('test1').column('frame'),
            sampled_increment: db.sampler.gather([0, 10, 25, 26, 27]),
            output_op: 'test_slicing',
        })

    tables = db.run(output_op, [job], force=True, show_progress=False)

    num_rows = 0
    expected_output = [0, warmup, warmup, warmup + 1, warmup + 2]
    for buf in tables[0].column('integer').load():
        (val, ) = struct.unpack('=q', buf)
        assert val == expected_output[num_rows]
        print(num_rows)
        num_rows += 1
    assert num_rows == 5


def test_unbounded_state(db):
    frame = db.sources.FrameColumn()
    slice_frame = frame.slice()
    increment = db.ops.TestIncrementUnbounded(ignore=slice_frame)
    unsliced_increment = increment.unslice()
    output_op = db.sinks.Column(columns={'integer': unsliced_increment})
    job = Job(
        op_args={
            frame: db.table('test1').column('frame'),
            slice_frame: db.partitioner.all(50),
            output_op: 'test_slicing',
        })

    tables = db.run(output_op, [job], force=True, show_progress=False)

    num_rows = 0
    for _ in tables[0].column('integer').load():
        num_rows += 1
    assert num_rows == db.table('test1').num_rows()


def builder(cls):
    inst = cls()

    class Generated:
        def test_cpu(self, db):
            inst.run(db, inst.job(db, DeviceType.CPU))

        @gpu
        def test_gpu(self, db):
            inst.run(db, inst.job(db, DeviceType.GPU))

    return Generated


@builder
class TestHistogram:
    def job(self, db, ty):
        frame = db.sources.FrameColumn()
        hist = db.ops.Histogram(frame=frame, device=ty)
        output_op = db.sinks.Column(columns={'histogram': hist})

        job = Job(op_args={
            frame: db.table('test1').column('frame'),
            output_op: 'test_hist'
        })

        return output_op, [job]

    def run(self, db, job):
        tables = db.run(job[0], job[1], force=True, show_progress=False)
        next(tables[0].column('histogram').load(readers.histograms))


@builder
class TestOpticalFlow:
    def job(self, db, ty):
        frame = db.sources.FrameColumn()
        flow = db.ops.OpticalFlow(frame=frame, stencil=[-1, 0], device=ty)
        flow_range = flow.sample()
        out = db.sinks.Column(columns={'flow': flow_range})
        job = Job(
            op_args={
                frame: db.table('test1').column('frame'),
                flow_range: db.sampler.range(0, 50),
                out: 'test_flow',
            })
        return out, [job]

    def run(self, db, job):
        [table] = db.run(job[0], job[1], force=True, show_progress=False)
        num_rows = 0
        for _ in table.column('flow').load():
            num_rows += 1
        assert num_rows == 50

        flows = next(table.load(['flow']))
        flow_array = flows[0]
        assert flow_array.dtype == np.float32
        assert flow_array.shape[0] == 480
        assert flow_array.shape[1] == 640
        assert flow_array.shape[2] == 2


def test_stencil(db):
    frame = db.sources.FrameColumn()
    sample_frame = frame.sample()
    flow = db.ops.OpticalFlow(frame=sample_frame, stencil=[-1, 0])
    output_op = db.sinks.Column(columns={'flow': flow})
    job = Job(
        op_args={
            frame: db.table('test1').column('frame'),
            sample_frame: db.sampler.range(0, 1),
            output_op: 'test_sencil',
        })

    tables = db.run(
        output_op, [job],
        force=True,
        show_progress=False,
        pipeline_instances_per_node=1)

    num_rows = 0
    for _ in tables[0].column('flow').load():
        num_rows += 1
    assert num_rows == 1

    frame = db.sources.FrameColumn()
    sample_frame = frame.sample()
    flow = db.ops.OpticalFlow(frame=sample_frame, stencil=[0, 1])
    output_op = db.sinks.Column(columns={'flow': flow})
    job = Job(
        op_args={
            frame: db.table('test1').column('frame'),
            sample_frame: db.sampler.range(0, 1),
            output_op: 'test_sencil',
        })

    tables = db.run(
        output_op, [job],
        force=True,
        show_progress=False,
        pipeline_instances_per_node=1)

    frame = db.sources.FrameColumn()
    sample_frame = frame.sample()
    flow = db.ops.OpticalFlow(frame=sample_frame, stencil=[0, 1])
    output_op = db.sinks.Column(columns={'flow': flow})
    job = Job(
        op_args={
            frame: db.table('test1').column('frame'),
            sample_frame: db.sampler.range(0, 2),
            output_op: 'test_sencil',
        })

    tables = db.run(
        output_op, [job],
        force=True,
        show_progress=False,
        pipeline_instances_per_node=1)

    num_rows = 0
    for _ in tables[0].column('flow').load():
        num_rows += 1
    assert num_rows == 2

    frame = db.sources.FrameColumn()
    flow = db.ops.OpticalFlow(frame=frame, stencil=[-1, 0])
    sample_flow = flow.sample()
    output_op = db.sinks.Column(columns={'flow': sample_flow})
    job = Job(
        op_args={
            frame: db.table('test1').column('frame'),
            sample_flow: db.sampler.range(0, 1),
            output_op: 'test_sencil',
        })

    tables = db.run(
        output_op, [job],
        force=True,
        show_progress=False,
        pipeline_instances_per_node=1)

    num_rows = 0
    for _ in tables[0].column('flow').load():
        num_rows += 1
    assert num_rows == 1


def test_wider_than_packet_stencil(db):
    frame = db.sources.FrameColumn()
    sample_frame = frame.sample()
    flow = db.ops.OpticalFlow(frame=sample_frame, stencil=[0, 1])
    output_op = db.sinks.Column(columns={'flow': flow})
    job = Job(
        op_args={
            frame: db.table('test1').column('frame'),
            sample_frame: db.sampler.range(0, 3),
            output_op: 'test_sencil',
        })

    tables = db.run(
        output_op, [job],
        force=True,
        show_progress=False,
        io_packet_size=1,
        work_packet_size=1,
        pipeline_instances_per_node=1)

    num_rows = 0
    for _ in tables[0].column('flow').load():
        num_rows += 1
    assert num_rows == 3


def test_packed_file_source(db):
    # Write test file
    path = '/tmp/cpp_source_test'
    with open(path, 'wb') as f:
        num_elements = 4
        f.write(struct.pack('=Q', num_elements))
        # Write sizes
        for i in range(num_elements):
            f.write(struct.pack('=Q', 8))
        # Write data
        for i in range(num_elements):
            f.write(struct.pack('=Q', i))

    data = db.sources.PackedFile()
    pass_data = db.ops.Pass(input=data)
    output_op = db.sinks.Column(columns={'integer': pass_data})
    job = Job(op_args={
        data: {
            'path': path
        },
        output_op: 'test_cpp_source',
    })

    tables = db.run(output_op, [job], force=True, show_progress=False)

    num_rows = 0
    for buf in tables[0].column('integer').load():
        (val, ) = struct.unpack('=Q', buf)
        assert val == num_rows
        num_rows += 1
    assert num_elements == tables[0].num_rows()


def test_files_source(db):
    # Write test files
    path_template = '/tmp/files_source_test_{:d}'
    num_elements = 4
    paths = []
    for i in range(num_elements):
        path = path_template.format(i)
        with open(path, 'wb') as f:
            # Write data
            f.write(struct.pack('=Q', i))
        paths.append(path)

    data = db.sources.Files()
    pass_data = db.ops.Pass(input=data)
    output_op = db.sinks.Column(columns={'integer': pass_data})
    job = Job(op_args={
        data: {
            'paths': paths
        },
        output_op: 'test_files_source',
    })

    tables = db.run(output_op, [job], force=True, show_progress=False)

    num_rows = 0
    for buf in tables[0].column('integer').load():
        (val, ) = struct.unpack('=Q', buf)
        assert val == num_rows
        num_rows += 1
    assert num_elements == tables[0].num_rows()


def test_files_sink(db):
    # Write initial test files
    path_template = '/tmp/files_source_test_{:d}'
    num_elements = 4
    input_paths = []
    for i in range(num_elements):
        path = path_template.format(i)
        with open(path, 'wb') as f:
            # Write data
            f.write(struct.pack('=Q', i))
        input_paths.append(path)

    # Write output test files
    path_template = '/tmp/files_sink_test_{:d}'
    num_elements = 4
    output_paths = []
    for i in range(num_elements):
        path = path_template.format(i)
        output_paths.append(path)

    data = db.sources.Files()
    pass_data = db.ops.Pass(input=data)
    output_op = db.sinks.Files(input=pass_data)
    job = Job(op_args={
        data: {
            'paths': input_paths
        },
        output_op: {
            'paths': output_paths
        }
    })

    tables = db.run(output_op, [job], force=True, show_progress=False)

    # Read output test files
    for i in range(num_elements):
        path = output_paths[i]
        with open(path, 'rb') as f:
            # Write data
            d, = struct.unpack('=Q', f.read())
            assert d == i


class TestPyKernel(Kernel):
    def __init__(self, config, protobufs):
        self.protobufs = protobufs
        assert (config.args['kernel_arg'] == 1)
        self.x = 20
        self.y = 20

    def close(self):
        pass

    def new_stream(self, args):
        if args is None:
            return
        if 'x' in args:
            self.x = args['x']
        if 'y' in args:
            self.y = args['y']

    def execute(self, input_columns):
        point = {}
        point['x'] = self.x
        point['y'] = self.y
        return [pickle.dumps(point)]


def test_python_kernel(db):

    db.register_op('TestPy', [('frame', ColumnType.Video)], ['dummy'])
    db.register_python_kernel('TestPy', DeviceType.CPU, TestPyKernel)

    frame = db.sources.FrameColumn()
    range_frame = frame.sample()
    test_out = db.ops.TestPy(frame=range_frame, kernel_arg=1)
    output_op = db.sinks.Column(columns={'dummy': test_out})
    job = Job(
        op_args={
            frame: db.table('test1').column('frame'),
            range_frame: db.sampler.range(0, 30),
            output_op: 'test_hist'
        })

    tables = db.run(output_op, [job], force=True, show_progress=False)
    next(tables[0].load(['dummy']))


class TestPyBatchKernel(Kernel):
    def __init__(self, config, protobufs):
        self.protobufs = protobufs
        pass

    def close(self):
        pass

    def execute(self, input_columns):
        point = self.protobufs.Point()
        point.x = 10
        point.y = 5
        input_count = len(input_columns[0])
        column_count = len(input_columns)
        return [[point.SerializeToString() for _ in range(input_count)]
                for _ in range(column_count)]


def test_python_batch_kernel(db):

    db.register_op('TestPyBatch', [('frame', ColumnType.Video)], ['dummy'])
    db.register_python_kernel(
        'TestPyBatch', DeviceType.CPU, TestPyBatchKernel, batch=10)

    frame = db.sources.FrameColumn()
    range_frame = frame.sample()
    test_out = db.ops.TestPyBatch(frame=range_frame, batch=50)
    output_op = db.sinks.Column(columns={'dummy': test_out})
    job = Job(
        op_args={
            frame: db.table('test1').column('frame'),
            range_frame: db.sampler.range(0, 30),
            output_op: 'test_hist'
        })

    tables = db.run(output_op, [job], force=True, show_progress=False)
    next(tables[0].load(['dummy']))


def test_bind_op_args(db):
    try:
        db.register_op('TestPy', [('frame', ColumnType.Video)], ['dummy'])
        db.register_python_kernel('TestPy', DeviceType.CPU, TestPyKernel)
    except ScannerException:
        pass

    frame = db.sources.FrameColumn()
    range_frame = frame.sample()
    test_out = db.ops.TestPy(frame=range_frame, kernel_arg=1)
    output_op = db.sinks.Column(columns={'dummy': test_out})

    pairs = [(1, 5), (10, 50)]
    jobs = []
    for x, y in pairs:
        job = Job(
            op_args={
                frame: db.table('test1').column('frame'),
                test_out: {
                    'x': x,
                    'y': y
                },
                range_frame: db.sampler.range(0, 1),
                output_op: 'test_hist_{:d}'.format(x)
            })
        jobs.append(job)

    tables = db.run(output_op, jobs, force=True, show_progress=False)
    for i, (x, y) in enumerate(pairs):
        values = [v for v in tables[i].column('dummy').load()]
        p = pickle.loads(values[0])
        assert p['x'] == x
        assert p['y'] == y


def test_blur(db):
    frame = db.sources.FrameColumn()
    range_frame = frame.sample()
    blurred_frame = db.ops.Blur(frame=range_frame, kernel_size=3, sigma=0.1)
    output_op = db.sinks.Column(columns={'frame': blurred_frame})
    job = Job(
        op_args={
            frame: db.table('test1').column('frame'),
            range_frame: db.sampler.range(0, 30),
            output_op: 'test_blur',
        })

    tables = db.run(output_op, [job], force=True, show_progress=False)
    table = tables[0]

    frames = next(table.load(['frame']))
    frame_array = frames[0]
    assert frame_array.dtype == np.uint8
    assert frame_array.shape[0] == 480
    assert frame_array.shape[1] == 640
    assert frame_array.shape[2] == 3


def test_lossless(db):
    frame = db.sources.FrameColumn()
    range_frame = frame.sample()
    blurred_frame = db.ops.Blur(frame=range_frame, kernel_size=3, sigma=0.1)
    output_op = db.sinks.Column(columns={'frame': blurred_frame.lossless()})

    job = Job(
        op_args={
            frame: db.table('test1').column('frame'),
            range_frame: db.sampler.range(0, 30),
            output_op: 'test_blur_lossless'
        })

    tables = db.run(output_op, [job], force=True, show_progress=False)
    table = tables[0]
    next(table.load(['frame']))


def test_compress(db):
    frame = db.sources.FrameColumn()
    range_frame = frame.sample()
    blurred_frame = db.ops.Blur(frame=range_frame, kernel_size=3, sigma=0.1)
    compressed_frame = blurred_frame.compress('video', bitrate=1 * 1024 * 1024)
    output_op = db.sinks.Column(columns={'frame': compressed_frame})

    job = Job(
        op_args={
            frame: db.table('test1').column('frame'),
            range_frame: db.sampler.range(0, 30),
            output_op: 'test_blur_compressed'
        })

    tables = db.run(output_op, [job], force=True, show_progress=False)
    table = tables[0]
    next(table.load(['frame']))


def test_save_mp4(db):
    frame = db.sources.FrameColumn()
    range_frame = frame.sample()
    blurred_frame = db.ops.Blur(frame=range_frame, kernel_size=3, sigma=0.1)
    output_op = db.sinks.Column(columns={'frame': blurred_frame})

    job = Job(
        op_args={
            frame: db.table('test1').column('frame'),
            range_frame: db.sampler.range(0, 30),
            output_op: 'test_save_mp4'
        })

    tables = db.run(output_op, [job], force=True, show_progress=False)
    table = tables[0]
    f = tempfile.NamedTemporaryFile(delete=False, suffix='.mp4')
    f.close()
    table.column('frame').save_mp4(f.name)
    run(['rm', '-rf', f.name])


@pytest.fixture()
def no_workers_db():
    # Create new config
    (cfg_path, cfg) = make_config(master_port='5020', worker_port='5021')

    # Setup and ingest video
    with Database(debug=True, workers=[], config_path=cfg_path) as db:
        (vid1_path, vid2_path) = download_videos()

        db.ingest_videos([('test1', vid1_path), ('test2', vid2_path)])

        yield db

        # Tear down
        run([
            'rm', '-rf', cfg['storage']['db_path'], cfg_path, vid1_path,
            vid2_path
        ])


def test_no_workers(no_workers_db):
    db = no_workers_db

    frame = db.sources.FrameColumn()
    hist = db.ops.Histogram(frame=frame)
    output_op = db.sinks.Column(columns={'dummy': hist})

    job = Job(op_args={
        frame: db.table('test1').column('frame'),
        output_op: '_ignore'
    })

    exc = False
    try:
        output = db.run(output_op, [job], show_progress=False, force=True)
    except ScannerException:
        exc = True

    assert exc


@pytest.fixture()
def fault_db():
    # Create new config
    (cfg_path, cfg) = make_config(
        master_port='5010', worker_port='5011', path='/tmp/config_test')

    # Setup and ingest video
    with Database(
            master='localhost:5010',
            workers=[],
            config_path=cfg_path,
            no_workers_timeout=120) as db:
        (vid1_path, vid2_path) = download_videos()

        db.ingest_videos([('test1', vid1_path), ('test2', vid2_path)])

        yield db

        # Tear down
        run([
            'rm', '-rf', cfg['storage']['db_path'], cfg_path, vid1_path,
            vid2_path
        ])


# def test_clean_worker_shutdown(fault_db):
#     spawn_port = 5010
#     def worker_shutdown_task(config, master_address, worker_address):
#         from scannerpy import ProtobufGenerator, Config, start_worker
#         import time
#         import grpc
#         import subprocess

#         c = Config(None)

#         import scanner.metadata_pb2 as metadata_types
#         import scanner.engine.rpc_pb2 as rpc_types
#         import scanner.types_pb2 as misc_types
#         import libscanner as bindings

#         protobufs = ProtobufGenerator(config)

#         # Wait to kill worker
#         time.sleep(8)
#         # Kill worker
#         channel = grpc.insecure_channel(
#             worker_address,
#             options=[('grpc.max_message_length', 24499183 * 2)])
#         worker = protobufs.WorkerStub(channel)

#         try:
#             worker.Shutdown(protobufs.Empty())
#         except grpc.RpcError as e:
#             status = e.code()
#             if status == grpc.StatusCode.UNAVAILABLE:
#                 print('could not shutdown worker!')
#                 exit(1)
#             else:
#                 raise ScannerException('Worker errored with status: {}'
#                                        .format(status))

#         # Wait a bit
#         time.sleep(15)
#         script_dir = os.path.dirname(os.path.realpath(__file__))
#         subprocess.call(['python ' +  script_dir + '/spawn_worker.py'],
#                         shell=True)

#     master_addr = fault_db._master_address
#     worker_addr = fault_db._worker_addresses[0]
#     shutdown_process = Process(target=worker_shutdown_task,
#                              args=(fault_db.config, master_addr, worker_addr))
#     shutdown_process.daemon = True
#     shutdown_process.start()

#     frame = fault_db.sources.FrameColumn()
#     range_frame = frame.sample()
#     sleep_frame = fault_db.ops.SleepFrame(ignore = range_frame)
#     output_op = fault_db.sinks.Column(columns=[sleep_frame])

#     job = Job(
#         op_args={
#             frame: fault_db.table('test1').column('frame'),
#             range_frame: fault_db.sampler.range(0, 15),
#             output_op: 'test_shutdown',
#         }
#     )
#
#     table = fault_db.run(output_op, [job], pipeline_instances_per_node=1, force=True,
#                          show_progress=False)
#     table = table[0]
#     assert len([_ for _, _ in table.column('dummy').load()]) == 15

#     # Shutdown the spawned worker
#     channel = grpc.insecure_channel(
#         'localhost:' + str(spawn_port),
#         options=[('grpc.max_message_length', 24499183 * 2)])
#     worker = fault_db.protobufs.WorkerStub(channel)

#     try:
#         worker.Shutdown(fault_db.protobufs.Empty())
#     except grpc.RpcError as e:
#         status = e.code()
#         if status == grpc.StatusCode.UNAVAILABLE:
#             print('could not shutdown worker!')
#             exit(1)
#         else:
#             raise ScannerException('Worker errored with status: {}'
#                                    .format(status))
#     shutdown_process.join()


def test_fault_tolerance(fault_db):
    force_kill_spawn_port = 5012
    normal_spawn_port = 5013

    def worker_killer_task(config, master_address):
        from scannerpy import ProtobufGenerator, Config, start_worker
        import time
        import grpc
        import subprocess
        import signal
        import os

        c = Config(None)

        import scanner.metadata_pb2 as metadata_types
        import scanner.engine.rpc_pb2 as rpc_types
        import scanner.types_pb2 as misc_types
        import scannerpy.libscanner as bindings

        protobufs = ProtobufGenerator(config)

        # Spawn a worker that we will force kill
        script_dir = os.path.dirname(os.path.realpath(__file__))
        with open(os.devnull, 'w') as fp:
            p = subprocess.Popen(
                [
                    'python3 ' + script_dir +
                    '/spawn_worker.py {:d}'.format(force_kill_spawn_port)
                ],
                shell=True,
                stdout=fp,
                stderr=fp,
                preexec_fn=os.setsid)

            # Wait a bit for the worker to do its thing
            time.sleep(10)

            # Force kill worker process to trigger fault tolerance
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            p.kill()
            p.communicate()

            # Wait for fault tolerance to kick in
            time.sleep(15)

            # Spawn the worker again
            subprocess.call(
                [
                    'python3 ' + script_dir +
                    '/spawn_worker.py {:d}'.format(normal_spawn_port)
                ],
                shell=True)

    master_addr = fault_db._master_address
    killer_process = Process(
        target=worker_killer_task, args=(fault_db.config, master_addr))
    killer_process.daemon = True
    killer_process.start()

    frame = fault_db.sources.FrameColumn()
    range_frame = frame.sample()
    sleep_frame = fault_db.ops.SleepFrame(ignore=range_frame)
    output_op = fault_db.sinks.Column(columns={'dummy': sleep_frame})

    job = Job(
        op_args={
            frame: fault_db.table('test1').column('frame'),
            range_frame: fault_db.sampler.range(0, 20),
            output_op: 'test_fault',
        })

    table = fault_db.run(
        output_op, [job],
        pipeline_instances_per_node=1,
        force=True,
        show_progress=False)
    table = table[0]

    assert len([_ for _ in table.column('dummy').load()]) == 20

    # Shutdown the spawned worker
    channel = grpc.insecure_channel(
        'localhost:' + str(normal_spawn_port),
        options=[('grpc.max_message_length', 24499183 * 2)])
    worker = fault_db.protobufs.WorkerStub(channel)

    try:
        worker.Shutdown(fault_db.protobufs.Empty())
    except grpc.RpcError as e:
        status = e.code()
        if status == grpc.StatusCode.UNAVAILABLE:
            print('could not shutdown worker!')
            exit(1)
        else:
            raise ScannerException('Worker errored with status: {}'
                                   .format(status))
    killer_process.join()


@pytest.fixture()
def blacklist_db():
    # Create new config
    (cfg_path, cfg) = make_config(master_port='5055', worker_port='5060')

    # Setup and ingest video
    master = 'localhost:5055'
    workers = ['localhost:{:04d}'.format(5060 + d) for d in range(4)]
    with Database(
            config_path=cfg_path,
            no_workers_timeout=120,
            master=master,
            workers=workers) as db:
        (vid1_path, vid2_path) = download_videos()

        db.ingest_videos([('test1', vid1_path), ('test2', vid2_path)])

        yield db

        # Tear down
        run([
            'rm', '-rf', cfg['storage']['db_path'], cfg_path, vid1_path,
            vid2_path
        ])


class TestPyFailKernel(Kernel):
    def __init__(self, config, protobufs):
        self.protobufs = protobufs
        pass

    def close(self):
        pass

    def execute(self, input_columns):
        raise scannerpy.ScannerException('Test')


def test_job_blacklist(blacklist_db):
    db = blacklist_db
    db.register_op('TestPyFail', [('frame', ColumnType.Video)], ['dummy'])
    db.register_python_kernel('TestPyFail', DeviceType.CPU, TestPyFailKernel)

    frame = db.sources.FrameColumn()
    range_frame = frame.sample()
    failed_output = db.ops.TestPyFail(frame=range_frame)
    output_op = db.sinks.Column(columns={'dummy': failed_output})

    job = Job(
        op_args={
            frame: db.table('test1').column('frame'),
            range_frame: db.sampler.range(0, 1),
            output_op: 'test_py_fail'
        })

    tables = db.run(
        output_op, [job],
        force=True,
        show_progress=False,
        pipeline_instances_per_node=1)
    table = tables[0]
    assert table.committed() == False


@pytest.fixture()
def timeout_db():
    # Create new config
    (cfg_path, cfg) = make_config(master_port='5155', worker_port='5160')

    # Setup and ingest video
    master = 'localhost:5155'
    workers = ['localhost:{:04d}'.format(5160 + d) for d in range(4)]
    with Database(
            config_path=cfg_path,
            no_workers_timeout=120,
            master=master,
            workers=workers) as db:
        (vid1_path, vid2_path) = download_videos()

        db.ingest_videos([('test1', vid1_path), ('test2', vid2_path)])

        yield db

        # Tear down
        run([
            'rm', '-rf', cfg['storage']['db_path'], cfg_path, vid1_path,
            vid2_path
        ])


def test_job_timeout(timeout_db):
    db = timeout_db

    frame = db.sources.FrameColumn()
    range_frame = frame.sample()
    sleep_frame = db.ops.SleepFrame(ignore=range_frame)
    output_op = db.sinks.Column(columns={'dummy': sleep_frame})

    job = Job(
        op_args={
            frame: db.table('test1').column('frame'),
            range_frame: db.sampler.range(0, 1),
            output_op: 'test_timeout',
        })

    table = db.run(
        output_op, [job],
        pipeline_instances_per_node=1,
        task_timeout=0.1,
        force=True,
        show_progress=False)
    table = table[0]

    assert table.committed() == False
