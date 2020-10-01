import logging
import json
from functools import reduce
from typing import TypedDict, List

from airflow.hooks.http_hook import HttpHook
from airflow.operators.bash_operator import BaseOperator
from airflow.utils.decorators import apply_defaults


class UpsertFreshnessMetricOperator(BaseOperator):

    # Only for Python 3.8+
    # TODO - find a way to check what Python version is running
    # class FreshnessConfig(TypedDict, total=False):
    #     schema_name: str
    #     table_name: str
    #     column_name: str
    #     hours_between_update: int
    #     hours_delay_at_update: int
    #     notifications: List[str]
    #     default_check_frequency_hours: int

    @apply_defaults
    def __init__(self,
                 connection_id: str,
                 warehouse_id: int,
                 configuration: dict(schema_name=None, table_name=None, column_name=None,
                                     hours_between_update=None, hours_delay_at_update=None,
                                     extras=...),
                 *args,
                 **kwargs):
        super(UpsertFreshnessMetricOperator, self).__init__(*args, **kwargs)
        self.connection_id = connection_id
        self.warehouse_id = warehouse_id
        self.configuration = configuration

    def execute(self, context):
        for c in self.configuration:
            table_name = c["table_name"]
            schema_name = c["schema_name"]
            column_name = c["column_name"]
            hours_between_update = c["hours_between_update"]
            hours_delay_at_update = c["hours_delay_at_update"]
            default_check_frequency_hours = c.get("default_check_frequency_hours", 2)
            notifications = c.get("notifications", [])
            table = self._get_table_for_name(schema_name, table_name)
            if table is None or table.get("id") is None:
                raise Exception("Could not find table: ", self.schema_name, self.table_name)
            existing_metric = self._get_existing_freshness_metric(table)
            metric = self._get_metric_object(existing_metric, table, notifications, column_name,
                                             hours_between_update, hours_delay_at_update, default_check_frequency_hours)
            logging.info("Sending metric to create: ", metric)
            hook = self.get_hook('POST')
            result = hook.run("api/v1/metrics",
                              headers={"Content-Type": "application/json", "Accept": "application/json"},
                              data=json.dumps(metric))
            logging.info("Create metric status: ", result.status_code)
            logging.info("Create result: ", result.json())

    def get_hook(self, method) -> HttpHook:
        return HttpHook(http_conn_id=self.connection_id, method=method)

    def _get_metric_object(self, existing_metric, table, notifications, column_name,
                           hours_between_update, hours_delay_at_update, default_check_frequency_hours):
        metric = {
            "scheduleFrequency": {
                "intervalType": "HOURS_TIME_INTERVAL_TYPE",
                "intervalValue": default_check_frequency_hours
            },
            "thresholds": [
                {
                    "constantThreshold": {
                        "bound": {
                            "boundType": "UPPER_BOUND_SIMPLE_BOUND_TYPE",
                            "value": hours_between_update + hours_delay_at_update
                        }
                    }
                },
                {
                    "constantThreshold": {
                        "bound": {
                            "boundType": "LOWER_BOUND_SIMPLE_BOUND_TYPE",
                            "value": hours_delay_at_update
                        }
                    }
                }
            ],
            "warehouseId": self.warehouse_id,
            "datasetId": table.get("id"),
            "metricType": {
                "predefinedMetric": {
                    "metricName": self._get_metric_name_for_field(table, column_name)
                }
            },
            "parameters": [
                {
                    "key": "arg1",
                    "columnName": column_name
                }
            ],
            "lookback": {
                "intervalType": "DAYS_TIME_INTERVAL_TYPE",
                "intervalValue": 14
            },
            "notificationChannels": self._get_notification_channels(notifications)
        }
        if existing_metric is None:
            return metric
        else:
            existing_metric["thresholds"] = metric["thresholds"]
            existing_metric["notificationChannels"] = metric.get("notificationChannels", [])
            existing_metric["scheduleFrequency"] = metric["scheduleFrequency"]
            return existing_metric

    def _get_existing_freshness_metric(self, table):
        hook = self.get_hook('GET')
        result = hook.run("api/v1/metrics?warehouseIds={warehouse_id}&tableIds={table_id}"
                          .format(warehouse_id=self.warehouse_id,
                                  table_id=table.get("id")),
                          headers={"Accept": "application/json"})
        metrics = result.json()
        for m in metrics:
            if self._is_latency_metric(m):
                return m
        return None

    def _is_latency_metric(self, metric):
        keys = ["metricType", "predefinedMetric", "metricName"]
        result = reduce(lambda val, key: val.get(key) if val else None, keys, metric)
        return result is not None and result.startswith("HOURS_SINCE_MAX")

    def _get_table_for_name(self, schema_name, table_name):
        hook = self.get_hook('GET')
        result = hook.run("dataset/tables/{warehouse_id}/{schema_name}"
                          .format(warehouse_id=self.warehouse_id,
                                  schema_name=schema_name),
                          headers={"Accept": "application/json"})
        tables = result.json()
        for t in tables:
            if t['datasetName'].lower() == table_name.lower():
                return t
        return None

    def _get_notification_channels(self, notifications):
        channels = []
        for n in notifications:
            if n.startswith('#') or n.startswith('@'):
                channels.append({"slackChannel": n})
            elif n.contains('@') and n.contains('.'):
                channels.append({"email": n})
        return channels

    def _get_metric_name_for_field(self, table, column_name):
        for f in table.get("fields"):
            if f.get("fieldName").lower() == column_name.lower():
                if f.get("type") == "TIMESTAMP_LIKE":
                    return "HOURS_SINCE_MAX_TIMESTAMP"
                elif f.get("type") == "DATE_LIKE":
                    return "HOURS_SINCE_MAX_DATE"
