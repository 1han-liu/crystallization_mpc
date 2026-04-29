from typing import Optional
import time

try:
    from influxdb_client import InfluxDBClient, Point, WritePrecision
except Exception:
    InfluxDBClient = None
    Point = None
    WritePrecision = None

class InfluxSink:
    def __init__(self, url: str, token: str, org: str, bucket: str):
        self.enabled = InfluxDBClient is not None
        if self.enabled:
            self.client = InfluxDBClient(url=url, token=token, org=org)
            self.write = self.client.write_api()
            self.bucket = bucket
        else:
            print("[InfluxSink] influxdb-client not installed; sink disabled.")

    def write_point(self, measurement: str, tags: dict, fields: dict, ts_ns: Optional[int]=None):
        if not self.enabled:
            return
        p = Point(measurement)
        for k,v in tags.items():
            p = p.tag(k,v)
        for k,v in fields.items():
            p = p.field(k,v)
        if ts_ns is not None:
            p = p.time(ts_ns, WritePrecision.NS)
        self.write.write(bucket=self.bucket, record=p)

