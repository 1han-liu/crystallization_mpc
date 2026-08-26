# Interfaces and Data Paths

## Message Bus (RabbitMQ)

Exchange:
- idp.bus (type: topic, durable)

Queues:
- central.in
- controller.in
- gsensor.in

Bindings:
- {src}.to.{dst}
  - src/dst: central | controller | gsensor
- broadcast.#

## Controller clock boundary

RabbitMQ growth-rate messages update the latest-valid G cache; they do not call
the numerical controller. A monotonic scheduler creates one internal
`ControllerTickInput` every configured `dt` (baseline 5 s), containing the
tick sequence, elapsed time, optional cached G sample and age, and optional
process snapshot. Gsensor remains independent at `dt_G` (baseline 15 s), so the
normal tick-to-frame ratio is 3:1.

Experiment mode requires live G and OPC UA process reads. Simulation owns its
process state and can run without `ProcessState`. Read-only shadow uses
`CONTROLLER_OPCUA_ENABLED=true` and
`CONTROLLER_OPCUA_WRITE_ENABLED=false`.

## Time-Series Storage (InfluxDB)

Measurements:
- crystallizer
  - tags: line
  - fields: T, c_est, sigma, u
- gsensor_params
  - purpose: gsensor parameter snapshots and parameter-change events for Grafana correlation
  - tags: service, run_id, source, event, scope, param_key, value_type
  - tag values:
    - service: gsensor
    - source: default | central | ui | runtime
    - event: startup_snapshot | central_update | ui_apply | ui_reset | measurement_start_snapshot
    - scope: shared | gsensor
    - param_key: must exactly match the key in params_default.yaml, for example dt_G or params_G.width
    - value_type: float | string | bool | json
  - fields: value_float, value_string, value_bool, value_json, version, seq, changed

Planned (placeholders):
- sensor_g
  - tags: sensor_id
  - fields: G, quality
