#!/bin/bash
echo "insira o ramal desejado para o reboot"
read ramal
echo "verificando ip do ramal $ramal"
ip=`asterisk -rx "sip show peer $ramal" |grep Reg. |cut -c 22-50 |sed "s/$ramal@//g" | sed "s/:5060//g"`
echo "o ip é $ip"
sleep 2
echo "rebootando ramal $ramal"
curl -d pass=k1p3r POST http://$ip:8085
sleep 3
curl http://$ip:8085/A0
echo "rebootando ramal $ramal"
curl -d pass=k1p3r POST http://$ip:8085
sleep 3
curl http://$ip:8085/A0
echo "rebootando ramal $ramal"
curl -d pass=k1p3r POST http://$ip:8085
sleep 3
curl http://$ip:8085/A0

echo "E todos viveram felizes para sempre..."
