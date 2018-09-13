#!/usr/bin/env python
"""
This program is designed to stream COG-conversion and AWS uploads to be done same time
in a streaming fashion that utilises smaller in-between storage footprint. In raijin
this could be run in copyq in a PBS job. However, copyq lacks processing power, and therefore
it is advised that the job is run incrementally with appropriate increments. The program is
designed with vdi or raijin login nodes in mind.

The program utilizes a memory queue with an aligned file system queue of processed files to hook up
COG-conversion and AWS upload processes which are run on separate threads.

A particular batch run is identified by the signature (product, year, month) combination where
year and/or month may not be present. The job control tracks a particular batch job based on this signature
and the relevant log files are kept in the job control directory specified by the '--job' option (short, '-j').
There are two types of job control files kept, one with a template 'streamer_job_control_<signature>.log'
and one with a template 'items_all_<signature>.log', where the first tracks the batch run incrementally maintaining
the NetCDF files that are processed while the second keeps the list of all the files that are part of a specific
batch job. However, the script need to be told to keep 'items_all_<signature>.log' via the '--reuse_full_list'
option so that it does not need to recompute in the next incremental run of the same batch job
if it is not yet complete. The '--limit' option, which specifies the number of files to be processed,
can be used to incrementally run a specific batch job. Currently, the script is not designed
for multiple time overlapping runs.

For a specific product, the source NCI directory and the AWS directory within the bucket are hard coded.
The AWS bucket is specified via '--bucket' option. Therefore, the script currently limited to
'fc-ls5', 'fc-ls8', and 'wofs-wofls' products. However, additional products could be added easily
if they have similar 'grid-spec' (tiles) directory structure.

The following are the full list of options:
'--product', '-p', required=True, help="Product name: one of fc-ls5, fc-ls8, or wofs-wofls"
'--queue', '-q', required=True, help="Queue directory"
'--bucket', '-b', required=True, help="Destination Bucket Url"
'--job', '-j', required=True, help="Job directory that store job tracking info"
'--restart', is_flag=True, help="Restarts the job ignoring prior work"
'--year', '-y', type=click.INT, help="The year"
'--month', '-m', type=click.INT, help="The month"
'--limit', '-l', type=click.INT, help="Number of files to be processed in this run"
'--reuse_full_list', is_flag=True, help="Reuse the full file list for the signature(product, year, month)"
'--src', '-s',type=click.Path(exists=True), help="Source directory just above tiles directories. This option
                                                  must be used with --restart option"

The '--src' option, the source directory, is not meant to be used during production runs. It is there
for testing during dev stages.
"""
import threading
from concurrent.futures import ProcessPoolExecutor, wait, as_completed
from multiprocessing import Pool, Queue
import click
import os
from os.path import join as pjoin, basename, dirname, exists
import tempfile
import subprocess
from subprocess import check_call, run
import glob
from netCDF4 import Dataset
from datetime import datetime
from pandas import to_datetime
import gdal
import xarray
import yaml
from yaml import CLoader as Loader, CDumper as Dumper
from functools import reduce
import logging

LOG = logging.getLogger(__name__)

MAX_QUEUE_SIZE = 16
WORKERS_POOL = 7


def run_command(command, work_dir):
    """
    A simple utility to execute a subprocess command.
    """
    try:
        run(command, stderr=subprocess.STDOUT, cwd=work_dir, check=True)
    except subprocess.CalledProcessError as e:
        raise RuntimeError("command '{}' return with error (code {}): {}".format(e.cmd, e.returncode, e.output))


def upload_to_s3(file, src_dir, dest, job_file):
    """
    Uploads the .yaml and .tif files that correspond to the given NetCDF 'file' into the AWS
    destination bucket indicated by 'dest'. Once complete add the file name 'file' to the 'job_file'.
    Each NetCDF file is assumed to be a stacked NetCDF file where 'unstacked' NetCDF file is viewed as
    a stacked file with just one dataset. The directory structure in AWS is determined by the
    'JobControl.aws_dir()' based on 'prefixes' extracted from the NetCDF file. Each dataset within
    the NetCDF file is assumed to be in separate directory with the name indicated by its corresponding
    prefix. The 'prefix' would have structure as in 'LS_WATER_3577_9_-39_20180506102018000000'.
    """

    file_names = JobControl.get_unstacked_names(file)
    for index in range(len(file_names)):
        prefix = file_names[index]
        src = os.path.join(src_dir, prefix)
        item_dir = JobControl.aws_dir(prefix)
        dest_name = os.path.join(dest, item_dir)
        aws_copy1 = [
            'aws',
            's3',
            'sync',
            src,
            dest_name,
            '--exclude',
            '*',
            '--include',
            '{}*.yaml'.format(prefix)
        ]
        aws_copy2 = [
            'aws',
            's3',
            'sync',
            src,
            dest_name,
            '--exclude',
            '*',
            '--include',
            '{}*.tif'.format(prefix)
        ]
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                run_command(aws_copy1, tmpdir)
                run_command(aws_copy2, tmpdir)
        except Exception as e:
            logging.error("AWS upload error %s", prefix)
            logging.exception("Exception", e)

        # Remove the dir from the queue directory
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                run('rm -fR -- ' + src, stderr=subprocess.STDOUT, cwd=tmpdir, check=True, shell=True)
        except Exception as e:
            logging.error("Failure in queue: removing dataset %s", prefix)
            logging.exception("Exception", e)

    # job control logs
    with open(job_file, 'a') as f:
        f.write(file + '\n')


class COGNetCDF:
    """ Bunch of utilities for COG conversion of NetCDF files"""

    @staticmethod
    def _dataset_to_yaml(prefix, dataset, dest_dir):
        """ Refactored from Author Harshu Rampur's cog conversion scripts - Write the datasets to separate yaml files"""
        y_fname = os.path.join(dest_dir, prefix + '.yaml')
        dataset_object = dataset.decode('utf-8')
        dataset = yaml.load(dataset_object, Loader=Loader)

        # Update band urls
        for key, value in dataset['image']['bands'].items():
            value['layer'] = '1'
            value['path'] = prefix + '_' + key + '.tif'

        dataset['format'] = {'name': 'GeoTIFF'}
        dataset['lineage'] = {'source_datasets': {}}
        with open(y_fname, 'w') as fp:
            yaml.dump(dataset, fp, default_flow_style=False, Dumper=Dumper)
            logging.info("Writing dataset Yaml to %s", basename(y_fname))

    @staticmethod
    def _dataset_to_cog(prefix, subdatasets, num, dest_dir):
        """ Refactored from Author Harshu Rampur's cog conversion scripts - Write the datasets to separate cog files"""

        with tempfile.TemporaryDirectory() as tmpdir:
            for dts in subdatasets[:-1]:
                band_name = (dts[0].split(':'))[-1]
                out_fname = prefix + '_' + band_name + '.tif'
                try:
                    env = ['GDAL_DISABLE_READDIR_ON_OPEN=YES',
                           'CPL_VSIL_CURL_ALLOWED_EXTENSIONS=.tif']
                    subprocess.check_call(env, shell=True)

                    # copy to a tempfolder
                    temp_fname = pjoin(tmpdir, basename(out_fname))
                    to_cogtif = [
                        'gdal_translate',
                        '-of',
                        'GTIFF',
                        '-b',
                        str(num),
                        dts[0],
                        temp_fname]
                    run_command(to_cogtif, tmpdir)

                    # Add Overviews
                    # gdaladdo - Builds or rebuilds overview images.
                    # 2, 4, 8,16,32 are levels which is a list of integral overview levels to build.
                    add_ovr = [
                        'gdaladdo',
                        '-r',
                        'average',
                        '--config',
                        'GDAL_TIFF_OVR_BLOCKSIZE',
                        '512',
                        temp_fname,
                        '2',
                        '4',
                        '8',
                        '16',
                        '32']
                    run_command(add_ovr, tmpdir)

                    # Convert to COG
                    cogtif = [
                        'gdal_translate',
                        '-co',
                        'TILED=YES',
                        '-co',
                        'COPY_SRC_OVERVIEWS=YES',
                        '-co',
                        'COMPRESS=DEFLATE',
                        '-co',
                        'ZLEVEL=9',
                        '--config',
                        'GDAL_TIFF_OVR_BLOCKSIZE',
                        '512',
                        '-co',
                        'BLOCKXSIZE=512',
                        '-co',
                        'BLOCKYSIZE=512',
                        '-co',
                        'PREDICTOR=2',
                        '-co',
                        'PROFILE=GeoTIFF',
                        temp_fname,
                        out_fname]
                    run_command(cogtif, dest_dir)
                except Exception as e:
                    logging.error("Failure during COG conversion: %s", out_fname)
                    logging.exception("Exception", e)

    @staticmethod
    def datasets_to_cog(file, dest_dir):
        """
        Convert the datasets in the NetCDF file 'file' into 'dest_dir' where each dataset is in
        a separate directory with the name indicated by the dataset prefix. The prefix would look
        like 'LS_WATER_3577_9_-39_20180506102018000000'
        """

        file_names = JobControl.get_unstacked_names(file)
        dataset_array = xarray.open_dataset(file)
        dataset = gdal.Open(file, gdal.GA_ReadOnly)
        subdatasets = dataset.GetSubDatasets()
        for index in range(len(file_names)):
            prefix = file_names[index]
            dataset_item = dataset_array.dataset.item(index)
            dest = os.path.join(dest_dir, prefix)
            with tempfile.TemporaryDirectory() as tmpdir:
                run_command(['mkdir', dest], tmpdir)
            COGNetCDF._dataset_to_yaml(prefix, dataset_item, dest)
            COGNetCDF._dataset_to_cog(prefix, subdatasets, index + 1, dest)
        return file


class TileFiles:
    """ A utility class used by multiprocess routines to compute the NetCDF file list for a product"""

    def __init__(self, year=None, month=None):
        self.year = year
        self.month = month

    def process_tile_files(self, tile_dir):
        names = []
        for top, dirs, files in os.walk(tile_dir):
            for name in files:
                name_ = os.path.splitext(name)
                if name_[1] == '.nc':
                    full_name = os.path.join(top, name)
                    time_stamp = name_[0].split('_')[-2]
                    if self.year:
                        if int(time_stamp[0:4]) == self.year:
                            if self.month and len(time_stamp) >= 6:
                                if int(time_stamp[4:6]) == self.month:
                                    names.append(full_name)
                            else:
                                names.append(full_name)
                    else:
                        names.append(full_name)
            break
        return names


class JobControl:
    """
    Utilities and some hardcoded stuff for tracking and coding job info.
    """

    @staticmethod
    def wofs_wofls_src_dir():
        return '/g/data/fk4/datacube/002/WOfS/WOfS_25_2_1/netcdf'

    @staticmethod
    def fc_ls5_src_dir():
        return '/g/data/fk4/datacube/002/FC/LS5_TM_FC'

    @staticmethod
    def fc_ls8_src_dir():
        return '/g/data/fk4/datacube/002/FC/LS8_OLI_FC'

    @staticmethod
    def wofs_wofls_aws_top_level():
        return 'WOfS/WOFLs/v2.1.0/combined'

    @staticmethod
    def fc_ls5_aws_top_level():
        return 'fractional-cover/fc/v2.2.0/ls5'

    @staticmethod
    def fc_ls8_aws_top_level():
        return 'fractional-cover/fc/v2.2.0/ls8'

    @staticmethod
    def aws_dir(item):
        """ Given a prefix like 'LS_WATER_3577_9_-39_20180506102018000000' what is the AWS directory structure?"""
        item_parts = item.split('_')
        time_stamp = item_parts[-1]
        assert len(time_stamp) == 20, '{} does not have an acceptable timestamp'.format(item)
        year = time_stamp[0:4]
        month = time_stamp[4:6]
        day = time_stamp[6:8]

        y_index = item_parts[-2]
        x_index = item_parts[-3]
        return os.path.join('x_' + x_index, 'y_' + y_index, year, month, day)

    @staticmethod
    def get_unstacked_names(netcdf_file, year=None, month=None):
        """
        Return the dataset prefix names corresponding to each dataset within the given NetCDF file.
        """

        dts = Dataset(netcdf_file)
        file_id = os.path.splitext(basename(netcdf_file))[0]
        prefix = "_".join(file_id.split('_')[0:-2])
        stack_info = file_id.split('_')[-1]

        dts_times = dts.variables['time']
        names = []
        for index, dt in enumerate(dts_times):
            dt_ = datetime.fromtimestamp(dt)
            time_stamp = to_datetime(dt_).strftime('%Y%m%d%H%M%S%f')
            if year:
                if month:
                    if dt_.year == year and dt_.month == month:
                        names.append('{}_{}'.format(prefix, time_stamp))
                elif dt_.year == year:
                    names.append('{}_{}'.format(prefix, time_stamp))
            else:
                names.append('{}_{}'.format(prefix, time_stamp))
        return names

    @staticmethod
    def get_gridspec_files(src_dir, year=None, month=None):
        """
        Extract the NetCDF file list corresponding to 'grid-spec' product for the given year and month
        """

        names = []
        for tile_top, tile_dirs, tile_files in os.walk(src_dir):
            full_name_list = [os.path.join(tile_top, tile_dir) for tile_dir in tile_dirs]
            with Pool(8) as p:
                names = p.map(TileFiles(year, month).process_tile_files, full_name_list)
            break
        return reduce((lambda x, y: x + y), names)


class Streamer(object):
    def __init__(self, product, src_dir, queue_dir, bucket_url, job_dir, restart,
                 year=None, month=None, limit=None, reuse_full_list=None):
        self.src_dir = src_dir
        self.queue_dir = queue_dir

        # We are going to start with a empty queue_dir
        with tempfile.TemporaryDirectory() as tmpdir:
            run('rm -fR ' + os.path.join(self.queue_dir, '*'),
                stderr=subprocess.STDOUT, cwd=tmpdir, check=True, shell=True)

        self.dest_url = bucket_url
        self.job_dir = job_dir

        # Compute the name of job control files
        job_file = 'streamer_job_control' + '_' + product
        job_file = job_file + '_' + str(year) if year else job_file
        job_file = job_file + '_' + str(month) if year and month else job_file
        job_file = job_file + '.log'
        items_all_file = 'items_all' + '_' + product
        items_all_file = items_all_file + '_' + str(year) if year else items_all_file
        items_all_file = items_all_file + '_' + str(month) if year and month else items_all_file
        items_all_file = items_all_file + '.log'

        # if restart clear streamer_job_control.log
        job_file = os.path.join(self.job_dir, job_file)
        if restart and os.path.exists(job_file):
            with tempfile.TemporaryDirectory() as tmpdir:
                run_command(['rm', job_file], tmpdir)

        # Compute file list
        items_done = []
        if os.path.exists(job_file):
            with open(job_file) as f:
                items_done = f.read().splitlines()

        # If reuse_full_list items_all are read from a file if present
        # and subsequently save into a file if items are computed new
        items_all_file = os.path.join(self.job_dir, items_all_file)
        if reuse_full_list:
            if os.path.exists(items_all_file):
                with open(items_all_file) as f:
                    items_all = f.read().splitlines()
            else:
                items_all = JobControl.get_gridspec_files(self.src_dir, year, month)
                with open(items_all_file, 'a') as f:
                    for item in items_all:
                        f.write(item + '\n')
        else:
            items_all = JobControl.get_gridspec_files(self.src_dir, year, month)

        self.items = [item for item in items_all if item not in items_done]
        self.items.sort(reverse=True)

        # Enforce if limit
        if limit:
            self.items = self.items[0:limit]

        print(self.items.__str__() + ' to do')
        self.job_file = job_file

    def compute(self, processed_queue, executor):
        """ The function that runs in the COG conversion thread """

        while self.items:
            queue_capacity = MAX_QUEUE_SIZE - processed_queue.qsize()
            run_size = queue_capacity if len(self.items) > queue_capacity else len(self.items)
            futures = [executor.submit(COGNetCDF.datasets_to_cog, self.items.pop(),
                                       self.queue_dir) for _ in range(run_size)]
            for future in as_completed(futures):
                processed_queue.put(future.result())
        processed_queue.put(None)

    def upload(self, processed_queue, executor):
        """ The function that run in the file upload to AWS thread """
        
        while True:
            items_todo = [processed_queue.get(block=True, timeout=None) for _ in range(processed_queue.qsize())]
            futures = []
            while items_todo:
                # We need to pop from the front
                item = items_todo.pop(0)
                if item is None:
                    wait(futures)
                    return
                futures.append(executor.submit(upload_to_s3, item, self.queue_dir, self.dest_url, self.job_file))
            wait(futures)

    def run(self):
        processed_queue = Queue(maxsize=MAX_QUEUE_SIZE)
        with ProcessPoolExecutor(max_workers=WORKERS_POOL) as executor:
            producer = threading.Thread(target=self.compute, args=(processed_queue, executor))
            consumer = threading.Thread(target=self.upload, args=(processed_queue, executor))
            producer.start()
            consumer.start()
            producer.join()
            consumer.join()


@click.command()
@click.option('--product', '-p', required=True, help="Product name: one of fc-ls5, fc-ls8, or wofs-wofls")
@click.option('--queue', '-q', required=True, help="Queue directory")
@click.option('--bucket', '-b', required=True, help="Destination Bucket Url")
@click.option('--job', '-j', required=True, help="Job directory that store job tracking info")
@click.option('--restart', is_flag=True, help="Restarts the job ignoring prior work")
@click.option('--year', '-y', type=click.INT, help="The year")
@click.option('--month', '-m', type=click.INT, help="The month")
@click.option('--limit', '-l', type=click.INT, help="Number of files to be processed in this run")
@click.option('--reuse_full_list', is_flag=True,
              help="Reuse the full file list for the signature(product, year, month)")
@click.option('--src', '-s',type=click.Path(exists=True),
              help="Source directory just above tiles directories. This option must be used with --restart option")
def main(product, queue, bucket, job, restart, year, month, limit, reuse_full_list, src):
    assert product in ['fc-ls5', 'fc-ls8', 'wofs-wofls'], "Product name must be one of fc-ls5, fc-ls8, or wofs-wofls"

    src_dir = None
    bucket_url = None
    if product == 'fc-ls5':
        src_dir = JobControl.fc_ls5_src_dir()
        bucket_url = os.path.join(bucket, JobControl.fc_ls5_aws_top_level())
    elif product == 'fc-ls8':
        src_dir = JobControl.fc_ls8_src_dir()
        bucket_url = os.path.join(bucket, JobControl.fc_ls8_aws_top_level())
    elif product == 'wofs-wofls':
        src_dir = JobControl.wofs_wofls_src_dir()
        bucket_url = os.path.join(bucket, JobControl.wofs_wofls_aws_top_level())

    if src:
        assert restart, "--src must be used with --restart option"
        src_dir = src

    restart_ = True if restart else False
    streamer = Streamer(product, src_dir, queue, bucket_url, job, restart_, year, month, limit, reuse_full_list)
    streamer.run()


if __name__ == '__main__':
    main()
