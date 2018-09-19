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
import re

from datacube import Datacube
from datacube.model import Range

LOG = logging.getLogger(__name__)

MAX_QUEUE_SIZE = 16
WORKERS_POOL = 7

DEFAULT_CONFIG = """
products: 
    wofs_albers: 
        time_type: timed
        src_dir: /g/data/fk4/datacube/002/WOfS/WOfS_25_2_1/netcdf
        src_dir_type: tiled
        aws_dir: WOfS/WOFLs/v2.1.0/combined
        bucket:  s3://dea-public-data-dev
    wofs_filtered_summary:
        time_type: flat
        template: wofs_filtered_summary_{x}_{y}.nc
        src_dir: /g/data2/fk4/datacube/002/WOfS/WOfS_Filt_Stats_25_2_1/netcdf
        src_dir_type: flat
        aws_dir: WOfS/filtered_summary/v2.1.0/combined
        bucket: s3://dea-public-data-dev
    ls5_fc_albers:
        time_type: timed
        src_dir: /g/data/fk4/datacube/002/FC/LS5_TM_FC
        src_dir_type: tiled
        aws_dir: fractional-cover/fc/v2.2.0/ls5
        bucket: s3://dea-public-data-dev
    ls8_fc_albers:
        time_type: timed
        src_dir: /g/data/fk4/datacube/002/FC/LS8_OLI_FC
        src_dir_type: tiled
        aws_dir: fractional-cover/fc/v2.2.0/ls8
        bucket: s3://dea-public-data-dev
"""


def run_command(command, work_dir):
    """
    A simple utility to execute a subprocess command.
    """
    try:
        run(command, stderr=subprocess.STDOUT, cwd=work_dir, check=True)
    except subprocess.CalledProcessError as e:
        raise RuntimeError("command '{}' return with error (code {}): {}".format(e.cmd, e.returncode, e.output))


def upload_to_s3(product, job_control, file, src_dir, dest, job_file):
    """
    Uploads the .yaml and .tif files that correspond to the given NetCDF 'file' into the AWS
    destination bucket indicated by 'dest'. Once complete add the file name 'file' to the 'job_file'.
    Each NetCDF file is assumed to be a stacked NetCDF file where 'unstacked' NetCDF file is viewed as
    a stacked file with just one dataset. The directory structure in AWS is determined by the
    'JobControl.aws_dir()' based on 'prefixes' extracted from the NetCDF file. Each dataset within
    the NetCDF file is assumed to be in separate directory with the name indicated by its corresponding
    prefix. The 'prefix' would have structure as in 'LS_WATER_3577_9_-39_20180506102018000000'.
    """

    file_names = job_control.get_unstacked_names(product, file)
    success = True
    for index in range(len(file_names)):
        prefix = file_names[index]
        src = os.path.join(src_dir, prefix)
        item_dir = job_control.aws_dir(prefix, product)
        dest_name = os.path.join(dest, item_dir)

        # Lets remove *.xml files
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                run('rm -fR -- ' + src + '/*.xml', stderr=subprocess.STDOUT, cwd=tmpdir, check=True, shell=True)
        except Exception as e:
            success = False
            logging.error("Failure in queue: removing datasets *.xml")
            logging.exception("Exception", e)

        aws_copy = [
            'aws',
            's3',
            'sync',
            src,
            dest_name
        ]
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                run_command(aws_copy, tmpdir)
        except Exception as e:
            success = False
            logging.error("AWS upload error %s", prefix)
            logging.exception("Exception", e)

        # Remove the dir from the queue directory
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                run('rm -fR -- ' + src, stderr=subprocess.STDOUT, cwd=tmpdir, check=True, shell=True)
        except Exception as e:
            success = False
            logging.error("Failure in queue: removing dataset %s", prefix)
            logging.exception("Exception", e)

    # job control logs
    if success:
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
                        'nearest',
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
    def datasets_to_cog(product, job_control, file, dest_dir):
        """
        Convert the datasets in the NetCDF file 'file' into 'dest_dir' where each dataset is in
        a separate directory with the name indicated by the dataset prefix. The prefix would look
        like 'LS_WATER_3577_9_-39_20180506102018000000'
        """

        file_names = job_control.get_unstacked_names(product, file)
        dataset_array = xarray.open_dataset(file)
        dataset = gdal.Open(file, gdal.GA_ReadOnly)
        subdatasets = dataset.GetSubDatasets()
        for index in range(len(file_names)):
            prefix = file_names[index]
            dataset_item = dataset_array.dataset.item(index)
            dest = os.path.join(dest_dir, prefix)
            try:
                with tempfile.TemporaryDirectory() as tmpdir:
                    run_command(['mkdir', dest], tmpdir)
            except Exception as e:
                logging.error("Failure creating queue dir: %s", dest)
                logging.exception("Exception", e)
            else:
                COGNetCDF._dataset_to_yaml(prefix, dataset_item, dest)
                COGNetCDF._dataset_to_cog(prefix, subdatasets, index + 1, dest)
        return file


class TileFiles:
    """ A utility class used by multiprocess routines to compute the NetCDF file list for a product"""

    def __init__(self, year=None, month=None):
        self.year = year
        self.month = month

    @staticmethod
    def check(file, year, month):
        name, ext = os.path.splitext(basename(file))
        if ext == '.nc':
            time_stamp = name.split('_')[-2]
            if year:
                if int(time_stamp[0:4]) == year:
                    if month:
                        if len(time_stamp) >= 6 and int(time_stamp[4:6]) == month:
                            return True
                    else:
                        return True
            else:
                return True
        return False

    def process_tile_files(self, tile_dir):
        names = []
        for top, dirs, files in os.walk(tile_dir):
            for name in files:
                full_name = os.path.join(top, name)
                if self.check(full_name, self.year, self.month):
                    names.append(full_name)
            break
        return names


class JobControl:
    """
    Utilities and some hardcoded stuff for tracking and coding job info.
    """

    def __init__(self, cfg):
        self.cfg = cfg

    def aws_dir(self, item, product):
        """ Given a prefix like 'LS_WATER_3577_9_-39_20180506102018000000' what is the AWS directory structure?"""
        if self.cfg['products'][product]['time_type'] == 'flat':
            # only extract x and y
            tem = self.cfg['products'][product]['template']
            tem = tem.replace("{x}", "(?P<x>-?[0-9]*)")
            tem = tem.replace("{y}", "(?P<y>-?[0-9]*)")
            values = re.compile(tem).match(item + '.nc')
            return os.path.join('x_' + values['x'], 'y_' + values['y'])
        elif self.cfg['products'][product]['time_type'] == 'timed':
            item_parts = item.split('_')
            time_stamp = item_parts[-1]
            year = time_stamp[0:4]
            month = time_stamp[4:6]
            day = time_stamp[6:8]

            y_index = item_parts[-2]
            x_index = item_parts[-3]
            return os.path.join('x_' + x_index, 'y_' + y_index, year, month, day)
        else:
            raise RuntimeError("Incorrect product time_type")

    def get_unstacked_names(self, product, netcdf_file, year=None, month=None):
        """
        Return the dataset prefix names corresponding to each dataset within the given NetCDF file.
        """

        file_id = os.path.splitext(basename(netcdf_file))[0]
        names = []
        if self.cfg['products'][product]['time_type'] == 'flat':
            names.append(file_id)
        else:
            dts = Dataset(netcdf_file)
            prefix = "_".join(file_id.split('_')[0:-2])
            dts_times = dts.variables['time']
            for index, dt in enumerate(dts_times):
                dt_ = datetime.fromtimestamp(dt)
                # With nanosecond -use '%Y%m%d%H%M%S%f'
                time_stamp = to_datetime(dt_).strftime('%Y%m%d%H%M%S')
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
    def get_gridspec_files(src_dir, src_dir_type, year=None, month=None):
        """
        Extract the NetCDF file list corresponding to 'grid-spec' product for the given year and month
        """

        names = []
        for tile_top, tile_dirs, tile_files in os.walk(src_dir):
            if src_dir_type == 'tiled':
                full_name_list = [os.path.join(tile_top, tile_dir) for tile_dir in tile_dirs]
                with Pool(WORKERS_POOL) as p:
                    names = p.map(TileFiles(year, month).process_tile_files, full_name_list)
                    names = reduce((lambda x, y: x + y), names)
                break
            elif src_dir_type == 'flat':
                names = [os.path.join(tile_top, file) for file in tile_files if TileFiles.check(file, year, month)]
                break
        return names

    @staticmethod
    def get_indexed_files(product, year=None, month=None):
        query = {'product': product}
        if year and month:
            query['time'] = Range(datetime(year=year, month=month, day=1), datetime(year=year, month=month + 1, day=1))
        elif year:
            query['time'] = Range(datetime(year=year, month=1, day=1), datetime(year=year + 1, month=1, day=1))
        dc = Datacube(app='streamer', env='dea-prod')
        files = dc.index.datasets.search_returning(field_names=('uri',), **query)
        return [uri[0].split(':')[1] for uri in files]


class Streamer(object):
    def __init__(self, cfg, product, queue_dir, job_dir, restart,
                 year=None, month=None, limit=None, file_range=None, reuse_full_list=None, use_datacube=None):

        def _path_check(file, file_list):
            for item in file_list:
                if os.path.samefile(file, item):
                    return True
            return False

        self.product = product
        self.job_control = JobControl(cfg)
        self.queue_dir = queue_dir
        self.dest_url = os.path.join(cfg['products'][product]['bucket'], cfg['products'][product]['aws_dir'])
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

        # if restart clear streamer_job_control log and items_all log
        job_file = os.path.join(self.job_dir, job_file)
        items_all_file = os.path.join(self.job_dir, items_all_file)
        if restart:
            with tempfile.TemporaryDirectory() as tmpdir:
                if os.path.exists(job_file):
                    run_command(['rm', job_file], tmpdir)
                if os.path.exists(items_all_file):
                    run_command(['rm', items_all_file], tmpdir)

        # If reuse_full_list, items_all are read from a file if present
        # and save into a file if items are computed new
        if reuse_full_list:
            if os.path.exists(items_all_file):
                with open(items_all_file) as f:
                    items_all = f.read().splitlines()
            else:
                if use_datacube:
                    items_all = JobControl.get_indexed_files(product, year, month)
                else:
                    items_all = JobControl.get_gridspec_files(cfg['products'][product]['src_dir'],
                                                              cfg['products'][product]['src_dir_type'], year, month)
                with open(items_all_file, 'a') as f:
                    for item in items_all:
                        f.write(item + '\n')
        else:
            if use_datacube:
                items_all = JobControl.get_indexed_files(product, year, month)
            else:
                items_all = JobControl.get_gridspec_files(cfg['products'][product]['src_dir'],
                                                          cfg['products'][product]['src_dir_type'], year, month)

        if file_range:
            start_file, end_file = file_range
            # start_file and end_file are inclusive so we need end_file + 1
            self.items = items_all[start_file: end_file + 1]

            # We need the file system queue specific for this run
            self.queue_dir = os.path.join(self.queue_dir, product + '_range_run_{}_{}'.format(start_file, end_file))
        else:
            # Compute file list
            items_done = []
            if os.path.exists(job_file):
                with open(job_file) as f:
                    items_done = f.read().splitlines()

            self.items = [item for item in items_all if not _path_check(item, items_done)]
            # self.items.sort(reverse=True)

            # Enforce if limit
            if limit:
                self.items = self.items[0:limit]

            # We don't want queue to have conflicts with other runs
            self.queue_dir = os.path.join(self.queue_dir, product + '_single_run')

        # We are going to start with a empty queue_dir
        if not os.path.exists(self.queue_dir):
            with tempfile.TemporaryDirectory() as tmpdir:
                run_command(['mkdir', self.queue_dir], tmpdir)
        else:
            with tempfile.TemporaryDirectory() as tmpdir:
                run('rm -fR ' + os.path.join(self.queue_dir, '*'),
                    stderr=subprocess.STDOUT, cwd=tmpdir, check=True, shell=True)

        print(self.items.__str__() + ' to do')
        self.job_file = job_file

    def compute(self, processed_queue, executor):
        """ The function that runs in the COG conversion thread """

        while self.items:
            queue_capacity = MAX_QUEUE_SIZE - processed_queue.qsize()
            run_size = queue_capacity if len(self.items) > queue_capacity else len(self.items)
            futures = [executor.submit(COGNetCDF.datasets_to_cog, self.product, self.job_control, self.items.pop(),
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
                futures.append(executor.submit(upload_to_s3, self.product, self.job_control, item,
                                               self.queue_dir, self.dest_url, self.job_file))
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
            # We will remove the queue directory
            with tempfile.TemporaryDirectory() as tmpdir:
                run('rm -fR ' + self.queue_dir, stderr=subprocess.STDOUT, cwd=tmpdir, check=True, shell=True)


@click.command()
@click.option('--product', '-p', required=True,
              help="Product name: one of ls5_fc_albers, ls8_fc_albers, wofs_albers, or wofs_filtered_summary")
@click.option('--queue', '-q', required=True, help="Queue directory")
@click.option('--job', '-j', required=True, help="Job directory that store job tracking info")
@click.option('--restart', is_flag=True, help="Restarts the job ignoring prior work")
@click.option('--year', '-y', type=click.INT, help="The year")
@click.option('--month', '-m', type=click.INT, help="The month")
@click.option('--limit', '-l', type=click.INT, help="Number of files to be processed in this run")
@click.option('--file_range', '-f', nargs=2, type=click.INT,
              help="The range of files (ends inclusive) with respect to full list")
@click.option('--reuse_full_list', is_flag=True,
              help="Reuse the full file list for the signature(product, year, month)")
@click.option('--use_datacube', is_flag=True, help="Use datacube to extract the list of files")
def main(product, queue, job, restart, year, month, limit, file_range, reuse_full_list, use_datacube):
    assert product in ['ls5_fc_albers', 'ls8_fc_albers', 'wofs_albers', 'wofs_filtered_summary'], \
        "Product name must be one of ls5_fc_albers, ls8_fc_albers, wofs_albers, or wofs_filtered_summary"

    cfg = yaml.load(DEFAULT_CONFIG)

    restart_ = True if restart else False
    streamer = Streamer(cfg, product, queue, job, restart_,
                        year, month, limit, file_range, reuse_full_list, use_datacube)
    streamer.run()


if __name__ == '__main__':
    main()
