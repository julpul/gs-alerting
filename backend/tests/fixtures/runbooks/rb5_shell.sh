#!/bin/bash
# pricing cache reset -- run from the jump host
ssh ops@{{instance}} 'sudo systemctl restart pricing-engine'
sleep 120
curl -s http://{{instance}}:8080/metrics | grep latency_p99
# if still > 1000 ms: page pricing on-call
