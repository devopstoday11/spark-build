#!/usr/bin/env python3

"""batch_test.py

Usage:
    batch_test.py <dispatcher_file> [options]

Arguments:
    dispatcher_file             file path to dispatchers list

Options:
    --docker-image <img>                              docker image to run on executors
    --group-role <group-role>                         root-level group to apply quotas against (e.g. '/dev') [default: None]
    --max-num-dispatchers <n>                         maximum number of dispatchers to use from dispatchers file
    --submits-per-min <n>                             number of jobs to submit per minute [default: 1]
    --spark-cores-max <n>                             max executor cores per job [default: 1]
    --spark-executor-cores <n>                        number of cores per executor [default: 1]
    --spark-port-max-retries <n>                      num of retries to find a driver UI port [default: 64]
    --spark-mesos-driver-failover-timeout <seconds>   driver failover timeout in seconds [default: 30]
    --spark-mesos-containerizer <containerizer>       containerizer for each driver [default: mesos]
    --spark-mesos-driver-labels <labels>              task labels to attach to each driver
    --spark-mesos-executor-gpus <n>                   number of gpus per executor
    --spark-mesos-max-gpus <n>                        max gpus per job
    --no-supervise                                    disable supervise mode
"""


from docopt import docopt
from threading import Thread

import json
import logging
import os
import random
import sys
import time
import typing

import sdk_utils
import spark_utils


# This script will submit jobs at a specified submit rate, alternating among the given
# set of dispatchers.
#
# Running:
# > dcos cluster setup <cluster url>
# > export PYTHONPATH=../spark-testing:../testing
# > python deploy-dispatchers.py 1 myspark dispatchers.txt
# > python batch_test.py dispatchers.txt


logging.basicConfig(
    format="[%(asctime)s|%(name)s|%(levelname)s]: %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)

log = logging.getLogger(__name__)
MONTE_CARLO_APP_URL = "https://raw.githubusercontent.com/mesosphere/spark-build/master/scale-tests/apps/monte-carlo-portfolio.py"
GPU_IMAGE_RECOGNITION_APP_URL = "https://raw.githubusercontent.com/mesosphere/spark-build/master/scale-tests/apps/image_recognition.py"


def _get_duration() -> int:
    """
    Randomly choose among a set of job durations in minutes according to a distribution.
    The average job duration is one hour.
    """
    rand = random.random()
    if rand < 0.239583:
        duration = 15
    elif rand < 0.479166:
        duration = 105
    elif rand < 0.979166:
        duration = 30
    else:
        duration = 720
    return duration


def _get_gpu_user_conf(args):
    def _verify_required_args():
        if not (
            args["--spark-mesos-max-gpus"]
            and args["--spark-mesos-executor-gpus"]
            and args["--docker-image"]
        ):
            log.error(
                """
            Missing required arguments for running gpu jobs. Please include:
            --spark-mesos-max-gpus
            --spark-mesos-executor-gpus
            --docker-image
            """
            )

    _verify_required_args()

    # Based on testing, 20gb per GPU is needed to run the job successfully.
    # This is due to memory being divvied up and allocated to each GPU device.
    memory_multiplier = 20
    memory = int(args["--spark-mesos-executor-gpus"]) * memory_multiplier
    return [
        "--conf",
        "spark.driver.memory={}g".format(str(memory)),
        "--conf",
        "spark.executor.memory={}g".format(str(memory)),
        "--conf",
        "spark.mesos.gpus.max={}".format(args["--spark-mesos-max-gpus"]),
        "--conf",
        "spark.mesos.executor.gpus={}".format(args["--spark-mesos-executor-gpus"]),
        "--conf",
        "spark.mesos.executor.docker.image={}".format(args["--docker-image"]),
        "--conf",
        "spark.mesos.executor.docker.forcePullImage=false",
    ]


def submit_job(
    app_url: str,
    app_args: str,
    dispatcher: typing.Dict,
    duration: int,
    config: typing.List[str],
    group_role: str,
):
    dispatcher_name = dispatcher["service"]["name"]
    log.info("Submitting job to dispatcher: %s, with duration: %s min.", dispatcher_name, duration)

    driver_role = None if group_role else dispatcher["roles"]["executors"]

    spark_utils.submit_job(
        service_name=dispatcher_name,
        app_url=app_url,
        app_args=app_args,
        verbose=False,
        args=config,
        driver_role=driver_role,
        spark_user=dispatcher["service"]["user"] if sdk_utils.is_strict_mode() else None,
        principal=dispatcher["service"]["service_account"] if sdk_utils.is_strict_mode() else None,
    )


def submit_loop(
    app_url: str,
    submits_per_min: int,
    dispatchers: typing.List[typing.Dict],
    user_conf: typing.List[str],
    group_role: str,
):
    sec_between_submits = 60 / submits_per_min
    log.info("sec_between_submits: %s", sec_between_submits)
    num_dispatchers = len(dispatchers)
    log.info("num_dispatchers: %s", num_dispatchers)

    dispatcher_index = 0
    while True:
        duration = _get_duration()

        if app_url == MONTE_CARLO_APP_URL:
            app_args = "100000 {}".format(str(duration * 30))  # about 30 iterations per min.
        else:
            app_args = "550 3"  # 550 images in 3 batches

        t = Thread(
            target=submit_job,
            args=(
                app_url,
                app_args,
                dispatchers[dispatcher_index],
                duration,
                user_conf,
                group_role,
            ),
        )
        t.start()
        dispatcher_index = (dispatcher_index + 1) % num_dispatchers
        log.info("sleeping %s sec.", sec_between_submits)
        time.sleep(sec_between_submits)


if __name__ == "__main__":
    args = docopt(__doc__)

    dispatchers = []
    with open(args["<dispatcher_file>"]) as f:
        data = json.load(f)
        dispatchers = data["spark"]

    if args["--max-num-dispatchers"]:
        end = int(args["--max-num-dispatchers"])
        if end <= len(dispatchers):
            dispatchers = dispatchers[0:end]
        else:
            log.warning(
                """
            Specified --max-num-dispatchers is greater than actual dispatcher count in {}.
            Using list of dispatchers from file instead.
            """.format(
                    args["<dispatcher_file>"]
                )
            )

    user_conf = [
        "--conf",
        "spark.cores.max={}".format(args["--spark-cores-max"]),
        "--conf",
        "spark.executor.cores={}".format(args["--spark-executor-cores"]),
        "--conf",
        "spark.mesos.containerizer={}".format(args["--spark-mesos-containerizer"]),
        "--conf",
        "spark.port.maxRetries={}".format(args["--spark-port-max-retries"]),
        "--conf",
        "spark.mesos.driver.failoverTimeout={}".format(
            args["--spark-mesos-driver-failover-timeout"]
        ),
    ]

    if args["--spark-mesos-executor-gpus"]:
        user_conf += _get_gpu_user_conf(args)
        MEMORY_MULTIPLIER = 20
        memory = int(args["--spark-mesos-executor-gpus"]) * MEMORY_MULTIPLIER
        user_conf += [
            "--conf",
            "spark.driver.memory={}g".format(str(memory)),
            "--conf",
            "spark.executor.memory={}g".format(str(memory)),
            "--conf",
            "spark.mesos.gpus.max={}".format(args["--spark-mesos-max-gpus"]),
            "--conf",
            "spark.mesos.executor.gpus={}".format(args["--spark-mesos-executor-gpus"]),
            "--conf",
            "spark.mesos.executor.docker.image={}".format(args["--docker-image"]),
            "--conf",
            "spark.mesos.executor.docker.forcePullImage=false",
        ]
        app_url = GPU_IMAGE_RECOGNITION_APP_URL
    else:
        app_url = MONTE_CARLO_APP_URL

    if args["--spark-mesos-driver-labels"] is not None:
        user_conf += [
            "--conf",
            "spark.mesos.driver.labels={}".format(args["--spark-mesos-driver-labels"]),
        ]

    if not args["--no-supervise"]:
        user_conf += ["--supervise"]

    if args["--max-num-dispatchers"]:
        end = int(args["--max-num-dispatchers"])
        dispatchers = dispatchers[0:end]

    group_role = args["--group-role"]

    submit_loop(app_url, int(args["--submits-per-min"]), dispatchers, user_conf, group_role)
