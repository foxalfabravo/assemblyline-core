import time
import json
import uuid
import logging
from typing import List

from assemblyline.odm.messages.dispatching import WatchQueueMessage
from assemblyline.odm.messages.task import FileInfo, Task as ServiceTask
from assemblyline.odm.models.service import Service
from assemblyline.remote.datatypes import get_client
from assemblyline.remote.datatypes.queues.named import NamedQueue
from assemblyline.remote.datatypes.hash import Hash, ExpiringHash
from assemblyline.remote.datatypes.set import ExpiringSet
from assemblyline.remote.datatypes.counters import MetricCounter
from assemblyline.common import isotime, forge

from al_core.dispatching.scheduler import Scheduler
from al_core.dispatching.dispatch_hash import DispatchHash
from assemblyline import odm
from assemblyline.odm.models.error import Error
from assemblyline.odm.models.submission import Submission
import al_core.watcher


def service_queue_name(service):
    return 'service-queue-'+service


def make_watcher_list_name(sid):
    return 'dispatch-watcher-list-' + sid


def get_tag_set_name(task):
    return '/'.join((task.sid, task.file_info.sha256, 'tags'))


def get_submission_tags_name(task):
    return "st/%s/%s" % (task.parent_hash, task.file_info.sha256)


@odm.model()
class SubmissionTask(odm.Model):
    submission: Submission = odm.Compound(Submission)
    completed_queue = odm.Keyword(default_set=True)                     # Which queue to notify on completion


# TODO determine what parameters from the submission are actually used in task scheduling
#      and store them directly in the task so that the full submission does not need to be retrieved
#      until the finalization of the submission
@odm.model()
class FileTask(odm.Model):
    sid = odm.Keyword()
    parent_hash = odm.Optional(odm.Keyword())
    file_info: FileInfo = odm.Compound(FileInfo)
    depth = odm.Integer()
    max_files = odm.Integer()


FILE_QUEUE = 'dispatch-file'
SUBMISSION_QUEUE = 'dispatch-submission'
DISPATCH_TASK_HASH = 'dispatch-active-tasks'


class Dispatcher:

    def __init__(self, datastore, redis, redis_persist, logger):
        # Load the datastore collections that we are going to be using
        self.datastore = datastore
        self.log = logger
        self.submissions = datastore.submission
        self.results = datastore.result
        self.errors = datastore.error
        self.files = datastore.file

        # Create a config cache that will refresh config values periodically
        self.config = forge.get_config()

        # Build some utility classes
        self.scheduler = Scheduler(datastore, self.config)
        self.classification_engine = forge.get_classification()

        # Connect to all of our persistent redis structures
        self.redis = redis or get_client(
            db=self.config.core.redis.nonpersistent.db,
            host=self.config.core.redis.nonpersistent.host,
            port=self.config.core.redis.nonpersistent.port,
            private=False,
        )
        self.redis_persist = redis_persist or get_client(
            db=self.config.core.redis.persistent.db,
            host=self.config.core.redis.persistent.host,
            port=self.config.core.redis.persistent.port,
            private=False,
        )
        self.timeout_watcher = al_core.watcher.WatcherClient(self.redis_persist)

        self.submission_queue = NamedQueue(SUBMISSION_QUEUE, self.redis)
        self.file_queue = NamedQueue(FILE_QUEUE, self.redis)
        self._nonper_other_queues = {}
        self.active_tasks = ExpiringHash(DISPATCH_TASK_HASH, host=self.redis_persist)

        # Publish counters to the metrics sink.
        self.files_complete_counter = MetricCounter('dispatch.files_complete', self.redis)

    def volatile_named_queue(self, name: str) -> NamedQueue:
        if name not in self._nonper_other_queues:
            self._nonper_other_queues[name] = NamedQueue(name, self.redis)
        return self._nonper_other_queues[name]

    def dispatch_submission(self, task: SubmissionTask):
        """
        Find any files associated with a submission and dispatch them if they are
        not marked as in progress. If all files are finished, finalize the submission.

        Preconditions:
            - File exists in the filestore and file collection in the datastore
            - Submission is stored in the datastore
        """
        submission = task.submission
        sid = submission.sid

        if not self.active_tasks.exists(sid):
            self.log.debug(f"Starting submission {sid} for {submission.params.submitter}")
            self.active_tasks.add(sid, task.as_primitives())
        else:
            self.log.debug(f"Check if submission {sid} is complete")

        # Refresh the watch, this ensures that this function will be called again
        # if something goes wrong with one of the files, and it never finishes.
        self.timeout_watcher.touch(key=sid, timeout=self.config.core.dispatcher.timeout,
                                   queue=SUBMISSION_QUEUE, message={'sid': sid})

        # Refresh the quota hold
        if submission.params.quota_item and submission.params.submitter:
            self.log.debug(f"Submission {sid} counts toward quota for {submission.params.submitter}")
            Hash('submissions-' + submission.params.submitter, self.redis_persist).add(sid, isotime.now_as_iso())

        # Open up the file/service table for this submission
        dispatch_table = DispatchHash(submission.sid, self.redis)

        # Try to find all files, and extracted files
        max_files = len(submission.files) + submission.params.max_extracted
        unchecked_files = []
        for submission_file in submission.files:
            file_data = self.files.get(submission_file.sha256)
            if not file_data:
                self.log.error(f'Submission {submission.sid} tried to process missing file: {submission_file.sha256}.')
                continue

            unchecked_files.append(FileTask(dict(
                sid=sid,
                file_info=dict(
                    magic=file_data.magic,
                    md5=file_data.md5,
                    mime=file_data.mime,
                    sha1=file_data.sha1,
                    sha256=file_data.sha256,
                    size=file_data.size,
                    type=file_data.type,
                ),
                depth=0,
                max_files=max_files
            )))

        encountered_files = {file.sha256 for file in submission.files}
        pending_files = {}
        file_parents = {}

        # Track information about the results as we hit them
        max_score = None
        result_classifications = []

        # For each file, we will look through all its results, any extracted files,
        # found should be added to the unchecked files if they haven't been encountered already
        while unchecked_files:
            file_task = unchecked_files.pop()
            sha = file_task.file_info.sha256
            schedule = self.build_schedule(dispatch_table, submission, sha, file_task.file_info.type)

            while schedule:
                stage = schedule.pop(0)
                for service_name in stage:
                    service = self.scheduler.services.get(service_name)

                    # If the service is still marked as 'in progress'
                    runtime = time.time() - dispatch_table.dispatch_time(sha, service_name)
                    if runtime < service.timeout:
                        pending_files[sha] = file_task
                        continue

                    # It hasn't started, has timed out, or is finished, see if we have a result
                    result_row = dispatch_table.finished(sha, service_name)

                    # No result found, mark the file as incomplete
                    if not result_row:
                        pending_files[sha] = file_task
                        continue

                    # The process table is marked that a service has been abandoned due to errors
                    if result_row.is_error:
                        continue

                    if not submission.params.ignore_filtering and result_row.drop:
                        schedule.clear()

                    # The result should exist then, get all the sub-files
                    result = self.results.get(result_row.key)
                    if not result:
                        self.log.error(f"Service responded to dispatcher with missing result: {result_row.key}")
                        continue

                    for sub_file in result.response.extracted:
                        file_parents[sub_file.sha256] = file_parents.get(sub_file.sha256, []) + [sha]

                        if sub_file.sha256 in encountered_files:
                            continue

                        encountered_files.add(sub_file.sha256)
                        file_data = self.datastore.file.get(sub_file.sha256)

                        if not file_data:
                            self.log.debug(f'Extracted file {sub_file} excluded from {sid} could'
                                           f' be error or due to extraction limits.')
                            continue

                        unchecked_files.append(FileTask(dict(
                            sid=sid,
                            file_info=dict(
                                magic=file_data.magic,
                                md5=file_data.md5,
                                mime=file_data.mime,
                                sha1=file_data.sha1,
                                sha256=file_data.sha256,
                                size=file_data.size,
                                type=file_data.type,
                            ),
                            depth=-1,  # This will be set later
                            max_files=max_files,
                        )))

                    # Collect information about the result
                    if max_score is None:
                        max_score = result.result.score
                    else:
                        max_score = max(max_score, result.result.score)
                    result_classifications.append(result.classification)

        # Now that we have seen the entire file tree, we can recalculate the depth of each file in the tree
        depth_limit = self.config.submission.max_extraction_depth
        def file_depth(sha):
            # A root file won't have any parents in the dict
            if sha not in file_parents:
                return 0
            return min(file_depth(parent) for parent in file_parents[sha]) + 1

        # Apply the depths, and filter out those over the limit
        for file_task in pending_files.values():
            file_task.depth = file_depth(file_task.file_info.sha256)
        pending_files = {sha: ft for sha, ft in pending_files.items() if ft.depth < depth_limit}

        # Filter out files based on the extraction limits
        pending_files = {sha: ft for sha, ft in pending_files.items() if dispatch_table.add_file(sha, max_files)}

        # If there are pending files, then at least one service, on at least one
        # file isn't done yet, and hasn't been filtered by any of the previous few steps
        # poke those files
        if pending_files:
            self.log.debug(f"Dispatching {len(pending_files)} files for submission {sid}: {list(pending_files.keys())}")

            for file_task in pending_files.values():
                self.file_queue.push(file_task.as_primitives())
        else:
            self.log.debug(f"Finishing submission {sid} for {submission.params.submitter}")
            self.finalize_submission(task, result_classifications, max_score, len(encountered_files))

    def finalize_submission(self, task: SubmissionTask, result_classifications, max_score, file_count):
        """All of the services for all of the files in this submission have finished or failed.

        Update the records in the datastore, and flush the working data from redis.
        """
        submission = task.submission
        sid = submission.sid

        if submission.params.quota_item and submission.params.submitter:
            self.log.info(f"Submission {sid} no longer counts toward quota for {submission.params.submitter}")
            Hash('submissions-' + submission.params.submitter, self.redis_persist).pop(sid)

        # Pull in the classifications of results/produced by services
        classification = submission.params.classification
        for c12n in result_classifications:
            classification = classification.max(c12n)

        # Pull down the dispatch table and clear it from redis
        dispatch_table = DispatchHash(submission.sid, self.redis)
        all_results = dispatch_table.all_results()
        dispatch_table.delete()

        # Sort the errors out of the results
        errors = []
        results = []
        for row in all_results.values():
            for status in row.values():
                if status.is_error:
                    errors.append(status.key)
                elif status.bucket == 'result':
                    results.append(status.key)
                else:
                    self.log.warning(f"Unexpected service output bucket: {status.bucket}/{status.key}")

        # submission['original_classification'] = submission['classification']
        submission.classification = classification
        submission.error_count = len(errors)
        submission.errors = errors
        submission.file_count = file_count
        submission.results = results
        submission.max_score = max_score or 0  # Submissions with no results have no score
        submission.state = 'completed'
        submission.times.completed = isotime.now_as_iso()
        self.submissions.save(sid, submission)

        if task.completed_queue:
            self.volatile_named_queue(task.completed_queue).push(submission.as_primitives())

        # Send complete message to any watchers.
        watcher_list = ExpiringSet(make_watcher_list_name(sid), host=self.redis)
        for w in watcher_list.members():
            NamedQueue(w).push(WatchQueueMessage({'status': 'STOP'}).as_primitives())

        # Clear the timeout watcher
        watcher_list.delete()
        self.timeout_watcher.clear(sid)
        self.active_tasks.pop(sid)
        self.log.debug(f"Finished submission {sid} for {submission.params.submitter}")

    def dispatch_file(self, task: FileTask):
        """ Handle a message describing a file to be processed.

        This file may be:
            - A new submission or extracted file.
            - A file that has just completed a stage of processing.
            - A file that has not completed a a stage of processing, but this
              call has been triggered by a timeout or similar.

        If the file is totally new, we will setup a dispatch table, and fill it in.

        Once we make/load a dispatch table, we will dispatch whichever group the table
        shows us hasn't been completed yet.

        When we dispatch to a service, we check if the task is already in the dispatch
        queue. If it isn't proceed normally. If it is, check that the service is still online.
        """
        # Read the message content
        file_hash = task.file_info.sha256
        active_task = self.active_tasks.get(task.sid)

        if active_task is None:
            self.log.warning(f"Untracked submission is being processed: {task.sid}")
            return

        submission_task = SubmissionTask(active_task)
        submission = submission_task.submission
        now = time.time()
        self.log.debug(f"Dispatching:  {file_hash} at depth {task.depth} for {task.sid}")

        # Refresh the watch on the submission, we are still working on it
        self.timeout_watcher.touch(key=task.sid, timeout=self.config.core.dispatcher.timeout,
                                   queue=SUBMISSION_QUEUE, message={'sid': task.sid})

        # Open up the file/service table for this submission
        dispatch_table = DispatchHash(task.sid, self.redis)

        # Calculate the schedule for the file
        schedule = self.build_schedule(dispatch_table, submission, file_hash, task.file_info.type)
        started_stages = []
        # TODO HMGET the entire schedule's results here rather than one at a time later

        # Go through each round of the schedule removing complete/failed services
        # Break when we find a stage that still needs processing
        outstanding = {}
        score = 0
        errors = 0
        while schedule and not outstanding:
            stage = schedule.pop(0)
            started_stages.append(stage)

            for service_name in stage:
                service = self.scheduler.services.get(service_name)

                # Load the results, if there are no results, then the service must be dispatched later
                finished = dispatch_table.finished(file_hash, service_name)
                if not finished:
                    outstanding[service_name] = service
                    continue

                # If the service terminated in an error, count the error and continue
                if finished.is_error:
                    errors += 1
                    continue

                # if the service finished, count the score, and check if the file has been dropped
                score += finished.score
                if not submission.params.ignore_filtering and finished.drop:
                    schedule.clear()
                    if schedule:  # If there are still stages in the schedule, over write them for next time
                        dispatch_table.schedules.set(file_hash, started_stages)

        # Try to retry/dispatch any outstanding services
        if outstanding:
            self.log.debug(f"File {file_hash} is being sent to: {list(outstanding.keys())}")

            for service_name, service in outstanding.items():
                # Check if this submission has already dispatched this service, and hasn't timed out yet
                # NOTE: This isn't for checking if a service has failed due to time out or generating
                #       related errors. This is a DISPATCHING timer, to be sure that we don't send the
                #       same submission+file+service to the service queues too often. This is part of what
                #       makes repeated dispatching of a submission or file largely idempotent.
                queued_time = time.time() - dispatch_table.dispatch_time(file_hash, service_name)
                if queued_time < service.timeout:
                    continue

                config = self.build_service_config(service, submission)

                # Build the actual service dispatch message
                service_task = ServiceTask(dict(
                    sid=task.sid,
                    service_name=service_name,
                    service_config=json.dumps(config),
                    fileinfo=task.file_info,
                    depth=task.depth,
                    max_files=task.max_files,
                ))

                queue = self.volatile_named_queue(service_queue_name(service_name))
                queue.push(service_task.as_primitives())
                dispatch_table.dispatch(file_hash, service_name)

        else:
            # There are no outstanding services, this file is done

            # Store the file's score under this configuration into the
            filescore_key = submission.params.create_filescore_key(file_hash)
            self.datastore.filescore.save(filescore_key, {
                'psid': submission.params.psid if submission.params.psid else None,
                'expiry_ts': submission.expiry_ts,
                'score': score,
                'errors': errors,
                'sid': submission.sid,
                'time': now,
            })

            # Erase tags
            ExpiringSet(get_tag_set_name(task), host=self.redis).delete()
            ExpiringHash(get_submission_tags_name(task), host=self.redis).delete()

            # If there are no outstanding ANYTHING for this submission,
            # send a message to the submission dispatcher to finalize
            self.log.debug(f"Finished: {submission.sid}/{file_hash}")
            self.files_complete_counter.increment()
            if dispatch_table.all_finished():
                self.submission_queue.push({'sid': submission.sid})

    def build_schedule(self, dispatch_hash: DispatchHash, submission: Submission, file_hash, file_type) -> List[List[str]]:
        """Rather than rebuilding the schedule every time we see a file, build it once and cache in redis."""
        cached_schedule = dispatch_hash.schedules.get(file_hash)
        if not cached_schedule:
            # Get the schedule for that file type based on the submission parameters
            obj_schedule = self.scheduler.build_schedule(submission, file_type)
            # The schedule built by the scheduling tool has the service objects, we just want the names for now
            cached_schedule = [list(stage.keys()) for stage in obj_schedule]
            dispatch_hash.schedules.add(file_hash, cached_schedule)
        return cached_schedule

    def build_service_config(self, service: Service, submission: Submission):
        """Prepare the service config that will be used downstream.

        v3 names: get_service_params get_config_data
        """
        # Load the default service config
        params = {x.name: x.default for x in service.submission_params}

        # Over write it with values from the submission
        if service.name in submission.params.service_spec:
            params.update(submission.params.service_spec[service.name])
        return params

    # def heartbeat(self):
    #     while not self.drain:
    #         with self.lock:
    #             heartbeat = {
    #                 'shard': self.shard,
    #                 'entries': len(self.entries),
    #                 'errors': len(self.errors),
    #                 'results': len(self.results),
    #                 'resources': {
    #                     "cpu_usage.percent": psutil.cpu_percent(),
    #                     "mem_usage.percent": psutil.virtual_memory().percent,
    #                     "disk_usage.percent": psutil.disk_usage('/').percent,
    #                     "disk_usage.free": psutil.disk_usage('/').free,
    #                 },
    #                 'services': self._service_info(), 'queues': {
    #                     'max_inflight': self.high,
    #                     'control': self.control_queue.length(),
    #                     'ingest': q.length(self.ingest_queue),
    #                     'response': q.length(self.response_queue),
    #                 },
    #                 'hostinfo': self.hostinfo
    #             }
    #
    #             msg = message.Message(to="*", sender='dispatcher', mtype=message.MT_DISPHEARTBEAT, body=heartbeat)
    #             CommsQueue('status').publish(msg.as_dict())
    #
    #         time.sleep(1)
    #
    # # noinspection PyUnusedLocal
    # def interrupt(self, unused1, unused2):  # pylint: disable=W0613
    #     if self.drain:
    #         log.info('Forced shutdown.')
    #         self.running = False
    #         return
    #
    #     log.info('Shutting down gracefully...')
    #     # Rename control queue to 'control-<hostname>-<pid>-<seconds>-<shard>'.
    #     self.control_queue = \
    #         forge.get_control_queue('control-' + self.response_queue)
    #     self.drain = True
