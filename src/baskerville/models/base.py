# Copyright (c) 2020, eQualit.ie inc.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.


import abc
import datetime
import os
from collections import OrderedDict
from typing import List

import pyspark
from dateutil.tz import tzutc

from baskerville.db import get_jdbc_url
from baskerville.db.models import RequestSet
from baskerville.models.config import BaskervilleConfig
from baskerville.models.feature_manager import FeatureManager
from baskerville.spark.helpers import reset_spark_storage
from baskerville.spark.schemas import get_cache_schema
from baskerville.util.helpers import get_logger, TimeBucket, FOLDER_CACHE
from baskerville.models.request_set_cache import RequestSetSparkCache
from pyspark.sql import functions as F


class BaskervilleBase(object, metaclass=abc.ABCMeta):

    def __init__(self, config):
        self.config = config

    @abc.abstractmethod
    def run(self, *args, **kwargs):
        pass


class PipelineBase(object, metaclass=abc.ABCMeta):
    """
    The base contract for all pipelines:
    Basic attributes:
    - runtime
    - all_features
    - active_features
    - engine_conf
    - db_conf
    - db_url
    - step_to_action
    - remaining_steps
    - logs_df

    Contract methods:
    - run
    - finish_up: all pipelines must clean up after themselves
    """

    def __init__(self, db_conf, engine_conf, clean_up):
        self.runtime = None
        # todo: does not belong here anymore - see feature manager
        self.active_features = None
        self.step_to_action = None
        self.remaining_steps = None
        self.logs_df = None  # todo: remove once refactoring is done
        self.df = None
        self.db_conf = db_conf
        self.engine_conf = engine_conf
        self.all_features = self.engine_conf.all_features
        self.clean_up = clean_up
        self.db_url = get_jdbc_url(self.db_conf)
        self.logger = get_logger(
            self.__class__.__name__,
            logging_level=self.engine_conf.log_level,
            output_file=self.engine_conf.logpath
        )

    @abc.abstractmethod
    def run(self, *args, **kwargs):
        """
        For all defined steps, run the respective actions
        :param args:
        :param kwargs:
        :return:
        """
        for step, action in self.step_to_action.items():
            self.logger.info('Starting step {}'.format(step))
            action()
            self.logger.info('Completed step {}'.format(step))
            self.remaining_steps.remove(step)

    @abc.abstractmethod
    def initialize(self):
        pass

    @abc.abstractmethod
    def finish_up(self):
        pass


class TrainingPipelineBase(PipelineBase, metaclass=abc.ABCMeta):
    def __init__(self, db_conf, engine_conf, spark_conf, clean_up=True):
        super().__init__(db_conf, engine_conf, clean_up)
        self.data = None
        self.training_conf = self.engine_conf.training
        self.spark_conf = spark_conf
        self.feature_manager = FeatureManager(self.engine_conf)

    @abc.abstractmethod
    def get_data(self):
        pass

    @abc.abstractmethod
    def train(self):
        pass

    @abc.abstractmethod
    def test(self):
        pass

    @abc.abstractmethod
    def evaluate(self):
        pass

    @abc.abstractmethod
    def save(self, *args, **kwargs):
        pass

    def run(self, *args, **kwargs):
        super().run()


class Task(object, metaclass=abc.ABCMeta):
    name: str
    df: pyspark.sql.DataFrame
    steps: List['Task']
    config: BaskervilleConfig

    def __init__(self, config: BaskervilleConfig, steps: list = ()):
        self.df = None
        self.config = config
        self.steps = steps
        self.step_to_action = OrderedDict({
            str(step): step for step in self.steps
        })
        self.db_url = get_jdbc_url(self.config.database)
        self.logger = get_logger(
            self.__class__.__name__,
            logging_level=self.config.engine.log_level,
            output_file=self.config.engine.logpath
        )
        self.time_bucket = TimeBucket(self.config.engine.time_bucket)
        self.spark_task = RunnableSparkTaskWithCache(self.config)

    def __str__(self):
        return f'{self.__class__.__name__} with steps: ' \
               f'{",".join(step.__class__.__name__ for step in self.steps)}'

    @property
    def spark(self):
        return self.spark_task.spark

    @property
    def tools(self):
        return self.spark_task.tools

    @property
    def db_tools(self):
        return self.spark_task.tools

    @property
    def request_set_cache(self):
        return self.spark_task.request_set_cache

    @property
    def runtime(self):
        return self.spark_task.runtime

    @runtime.setter
    def runtime(self, value):
        self.spark_task.runtime = value

    def set_df(self, df):
        self.df = df
        return self

    def initialize(self):
        print(f'{self.__class__} initialize...')
        self.spark_task.initialize()
        # # do stuff
        # for step in self.steps:
        #     step.initialize()

    def run(self):
        """
        For all defined steps, run the respective actions
        :param args:
        :param kwargs:
        :return:
        """
        self.initialize()
        self.remaining_steps = list(self.step_to_action.keys())
        for descr, task in self.step_to_action.items():
            self.logger.info('Starting step {}'.format(descr))
            self.df = task.set_df(self.df).run()
            self.logger.info('Completed step {}'.format(descr))
            self.remaining_steps.remove(descr)
        return self.df

    def finish_up(self):
        pass

    def reset(self):
        self.spark_task.reset()

class TaskWithCache(Task):
    def __init__(self, config: BaskervilleConfig, steps: list = ()):
        super().__init__(config, steps)
        self.spark_task = RunnableSparkTaskWithCache(self.config)


class Singleton(object):
    def __init__(self, config):
        self.config = config

    def __new__(cls, *args, **kwargs):
        if not hasattr(cls, '_instance'):
            cls._instance = super(Singleton, cls).__new__(cls)
        return cls._instance


class Borg(object):
    _shared_state = {}

    def __init__(self, config):
        self.config = config

    def __new__(cls, *args, **kwargs):
        obj = super(Borg, cls).__new__(cls)
        obj.__dict__ = cls._shared_state
        return obj


class RunnableSparkTask(Borg):

    def __init__(self,
                 config: BaskervilleConfig):
        super().__init__(config)
        self.start_time = datetime.datetime.utcnow()
        self.request_set_cache = None
        self.spark = None
        self.tools = None
        self.spark_conf = self.config.spark
        self.db_url = get_jdbc_url(self.config.database)
        self.time_bucket = TimeBucket(self.config.engine.time_bucket)
        self.logger = get_logger(
            self.__class__.__name__,
            logging_level=self.config.engine.log_level,
            output_file=self.config.engine.logpath
        )

    def initialize(self):
        print(f'{self.__class__} initialize...')
        if not self.spark:
            from baskerville.spark import get_or_create_spark_session
            self.spark = get_or_create_spark_session(self.spark_conf)
        if not self.tools:
            from baskerville.util.baskerville_tools import BaskervilleDBTools
            self.tools = BaskervilleDBTools(self.config.database)
            self.tools.connect_to_db()

    def create_runtime(self):
        self.runtime = self.tools.create_runtime(
            start=self.start_time,
            conf=self.config.engine
        )

    def finish_up(self):
        """
        Unpersist all
        :return:
        """
        reset_spark_storage()

    def reset(self):
        """
        Unpersist rdds and dataframes and call GC - see broadcast memory
        release issue
        :return:
        """
        import gc

        reset_spark_storage()
        gc.collect()


class RunnableSparkTaskWithCache(RunnableSparkTask):
    def __init__(self,
                 config: BaskervilleConfig):
        super().__init__(config)
        self.request_set_cache = None
        self.cache_columns = [
            'target',
            'ip',
            'first_ever_request',
            'old_subset_count',
            'old_features',
            'old_num_requests',
        ]
        self.cache_config = {
            'db_url': self.db_url,
            'db_driver': self.spark_conf.db_driver,
            'user': self.config.database.user,
            'password': self.config.database.password
        }

    def initialize(self):
        """
        Set up an instance of RequestSetSparkCache using the cache
        configuration. Also, load past (start_time - expiry_time)
        if cache_load_past is configured.
        :return:
        """
        print(f'{self.__class__} initialize...')
        super().initialize()

        if not isinstance(self.request_set_cache, RequestSetSparkCache):
            self.request_set_cache = RequestSetSparkCache(
                cache_config=self.cache_config,
                table_name=RequestSet.__tablename__,
                columns_to_keep=(
                    'target',
                    'ip',
                    F.col('start').alias('first_ever_request'),
                    F.col('subset_count').alias('old_subset_count'),
                    F.col('features').alias('old_features'),
                    F.col('num_requests').alias('old_num_requests'),
                    F.col('updated_at')
                ),
                expire_if_longer_than=self.config.engine.cache_expire_time,
                path=os.path.join(self.config.engine.storage_path, FOLDER_CACHE)
            )
            if self.config.engine.cache_load_past:
                self.request_set_cache = self.request_set_cache.load(
                    update_date=(
                        self.start_time - datetime.timedelta(
                            seconds=self.config.engine.cache_expire_time
                        )
                    ).replace(tzinfo=tzutc()),
                    extra_filters=(
                            F.col('time_bucket') == self.time_bucket.sec
                    )  # todo: & (F.col("id_runtime") == self.runtime.id)?
                )
            else:
                self.request_set_cache.load_empty(get_cache_schema())

            self.logger.info(f'In cache: {self.request_set_cache.count()}')

            return self.request_set_cache

    def refresh_cache(self, df):
        """
        Update the cache with the current batch of logs in logs_df and clean up
        :return:
        """
        self.request_set_cache.update_self(df)
        df.unpersist()
        df = None
        # self.spark.catalog.clearCache()

    def filter_cache(self, df):
        """
        Use the current logs to find the past request sets - if any - in the
        request set cache
        :return:
        """
        df = df.select(
            F.col('client_request_host').alias('target'),
            F.col('client_ip').alias('ip'),
        ).distinct().alias('a').persist(self.spark_conf.storage_level)

        self.request_set_cache.filter_by(df)

        df.unpersist()
        del df

    def add_cache_columns(self, df):
        """
        Add columns from the cache to facilitate the
        feature extraction, prediction, and save processes
        :return:
        """
        df = df.alias('df')
        self.filter_cache(df)
        df = self.request_set_cache.update_df(
            df, select_cols=self.cache_columns
        )
        self.logger.debug(
            f'****** > # of rows in cache: {self.request_set_cache.count()}')
        return df
