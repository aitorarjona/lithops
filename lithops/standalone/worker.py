#
# (C) Copyright Cloudlab URV 2020
# (C) Copyright IBM Corp. 2024
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import os
import json
import redis
import flask
import logging
import signal
import subprocess as sp
from pathlib import Path
from threading import Thread
from functools import partial
from gevent.pywsgi import WSGIServer
from concurrent.futures import ThreadPoolExecutor

from lithops.utils import setup_lithops_logger
from lithops.standalone.keeper import BudgetKeeper
from lithops.standalone.utils import JobStatus, StandaloneMode, WorkerStatus
from lithops.constants import (
    CPU_COUNT,
    LITHOPS_TEMP_DIR,
    RN_LOG_FILE,
    SA_INSTALL_DIR,
    SA_LOG_FILE,
    JOBS_DIR,
    SA_CONFIG_FILE,
    SA_DATA_FILE,
    JOBS_PREFIX,
    SA_WORKER_SERVICE_PORT
)

log_format = "%(asctime)s\t[%(levelname)s] %(name)s:%(lineno)s -- %(message)s"
setup_lithops_logger(logging.DEBUG, filename=SA_LOG_FILE, log_format=log_format)
logger = logging.getLogger('lithops.standalone.worker')

app = flask.Flask(__name__)

redis_client = None
budget_keeper = None

job_processes = {}
worker_threads = {}


@app.route('/ping', methods=['GET'])
def ping():
    idle_count = sum(1 for worker in worker_threads.values() if worker['status'] == WorkerStatus.IDLE.value)
    busy_count = sum(1 for worker in worker_threads.values() if worker['status'] == WorkerStatus.BUSY.value)
    response = flask.jsonify({'busy': busy_count, 'free': idle_count})
    response.status_code = 200
    return response


@app.route('/stop/<job_key>', methods=['POST'])
def stop(job_key):
    logger.debug(f'Received SIGTERM: Stopping job process {job_key}')

    for job_key_call_id in job_processes:
        if job_key_call_id.startswith(job_key):
            PID = job_processes[job_key_call_id].pid
            PGID = os.getpgid(PID)
            logger.debug(f"Killing Job PID {PID}:{PGID}")
            os.killpg(PGID, signal.SIGKILL)
            Path(os.path.join(JOBS_DIR, job_key_call_id + '.done')).touch()
            job_processes[job_key_call_id] = None

    response = flask.jsonify({'response': 'cancel'})
    response.status_code = 200
    return response


def notify_worker_active(worker_name):
    try:
        redis_client.hset(f"worker:{worker_name}", 'status', WorkerStatus.ACTIVE.value)
    except Exception as e:
        logger.error(e)


def notify_worker_stop(worker_name):
    try:
        redis_client.hset(f"worker:{worker_name}", 'status',  WorkerStatus.STOPPED.value)
    except Exception as e:
        logger.error(e)


def notify_worker_delete(worker_name):
    try:
        redis_client.delete(f"worker:{worker_name}")
    except Exception as e:
        logger.error(e)


def notify_task_start(job_key, call_id):
    try:
        if redis_client.hget(f"job:{job_key}", 'status') == JobStatus.SUBMITTED.value:
            redis_client.hset(f"job:{job_key}", 'status', JobStatus.RUNNING.value)
    except Exception as e:
        logger.error(e)


def notify_task_done(job_key, call_id):
    try:
        done_tasks = int(redis_client.rpush(f"tasksdone:{job_key}", call_id))
        if int(redis_client.hget(f"job:{job_key}", 'total_tasks')) == done_tasks:
            redis_client.hset(f"job:{job_key}", 'status', JobStatus.DONE.value)
    except Exception as e:
        logger.error(e)


def redis_queue_consumer(pid, work_queue_name, exec_mode, local_job_dir, backend):
    global worker_threads

    worker_threads[pid]['status'] = WorkerStatus.IDLE.value

    logger.info(f"Redis consumer process {pid} started")

    while True:
        if exec_mode == StandaloneMode.REUSE.value:
            key, task_payload_str = redis_client.brpop(work_queue_name)
        else:
            task_payload_str = redis_client.rpop(work_queue_name)
            if task_payload_str is None:
                return

        task_payload = json.loads(task_payload_str)

        worker_threads[pid]['status'] = WorkerStatus.BUSY.value

        executor_id = task_payload['executor_id']
        job_id = task_payload['job_id']
        job_key = task_payload['job_key']
        call_id = task_payload['call_ids'][0]
        job_key_call_id = f'{job_key}-{call_id}'

        try:
            logger.debug(f'ExecutorID {executor_id} | JobID {job_id} - Running '
                         f'CallID {call_id} in the local worker')
            notify_task_start(job_key, call_id)
            budget_keeper.add_job(job_key_call_id)

            task_file = f'{job_key_call_id}-job.json'
            os.makedirs(local_job_dir, exist_ok=True)
            task_filename = os.path.join(local_job_dir, task_file)

            with open(task_filename, 'w') as jl:
                json.dump(task_payload, jl, default=str)

            cmd = ["python3", f"{SA_INSTALL_DIR}/runner.py", backend, task_filename]
            log = open(RN_LOG_FILE, 'a')
            process = sp.Popen(cmd, stdout=log, stderr=log, start_new_session=True)
            job_processes[job_key_call_id] = process
            process.communicate()  # blocks until the process finishes

            notify_task_done(job_key, call_id)
            logger.debug(f'ExecutorID {executor_id} | JobID {job_id} - CallID {call_id} execution finished')
            del job_processes[job_key_call_id]

        except Exception as e:
            logger.error(e)

        worker_threads[pid]['status'] = WorkerStatus.IDLE.value


def run_worker():
    global redis_client
    global budget_keeper
    global worker_threads

    os.makedirs(LITHOPS_TEMP_DIR, exist_ok=True)

    # read the Lithops standaole configuration file
    with open(SA_CONFIG_FILE, 'r') as cf:
        standalone_config = json.load(cf)

    # Read the VM data file that contains the instance id, the master IP,
    # and the queue for getting tasks
    with open(SA_DATA_FILE, 'r') as ad:
        vm_data = json.load(ad)

    # Start the redis client
    redis_client = redis.Redis(host=vm_data['master_ip'], decode_responses=True)

    # Set the worker as Active
    notify_worker_active(vm_data['name'])

    # Start the budget keeper. It is responsible to automatically terminate the
    # worker after X seconds
    if vm_data['master_ip'] != vm_data['private_ip']:
        stop_callback = partial(notify_worker_stop, vm_data['name'])
        delete_callback = partial(notify_worker_delete, vm_data['name'])
        budget_keeper = BudgetKeeper(standalone_config, stop_callback, delete_callback)
        budget_keeper.start()

    # Start the http server. This will be used by the master VM to pìng this
    # worker and for canceling tasks
    def run_wsgi():
        ip_address = "0.0.0.0" if os.getenv("DOCKER") == "Lithops" else vm_data['private_ip']
        server = WSGIServer((ip_address, SA_WORKER_SERVICE_PORT), app, log=app.logger)
        server.serve_forever()
    Thread(target=run_wsgi, daemon=True).start()

    # Start the consumer threads
    worker_processes = standalone_config[standalone_config['backend']]['worker_processes']
    worker_processes = CPU_COUNT if worker_processes == 'AUTO' else worker_processes
    logger.info(f"Starting Worker - Instace type: {vm_data['instance_type']} - Runtime "
                f"name: {standalone_config['runtime']} - Worker processes: {worker_processes}")

    local_job_dir = os.path.join(LITHOPS_TEMP_DIR, JOBS_PREFIX)
    os.makedirs(local_job_dir, exist_ok=True)

    # Create a ThreadPoolExecutor for cosnuming tasks
    redis_queue_consumer_futures = []
    with ThreadPoolExecutor(max_workers=worker_processes) as executor:
        for i in range(worker_processes):
            worker_threads[i] = {}
            future = executor.submit(
                redis_queue_consumer, i,
                vm_data.get('work_queue_name', 'wq:localhost'),
                standalone_config['exec_mode'],
                local_job_dir,
                standalone_config['backend']
            )
            redis_queue_consumer_futures.append(future)
            worker_threads[i]['future'] = future

        for future in redis_queue_consumer_futures:
            future.result()

    # run_worker will run forever in reuse mode. In create mode it will
    # run until there are no more tasks in the queue.
    logger.debug('Finished')

    try:
        # Try to stop the current worker VM once no more pending tasks to run
        # in case of create mode
        budget_keeper.stop_instance()
    except Exception:
        pass


if __name__ == '__main__':
    run_worker()
