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

## Time-Series Storage (InfluxDB)

Measurements:
- crystallizer
  - tags: line
  - fields: T, c_est, sigma, u

Planned (placeholders):
- sensor_g
  - tags: sensor_id
  - fields: G, quality
