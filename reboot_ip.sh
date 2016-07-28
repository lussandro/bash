#!/bin/bash
echo "ip informado $1"
curl -d pass=k1p3r POST http://$1:8085
sleep 3
curl http://$1:8085/A0
echo "rebootando ramal ip $1"
curl -d pass=k1p3r POST http://$1:8085
sleep 3
curl http://$1:8085/A0
echo "rebootando ramal ip $1"
curl -d pass=k1p3r POST http://$1:8085
sleep 3
curl http://$1:8085/A0

echo "E todos viveram felizes para sempre..."
