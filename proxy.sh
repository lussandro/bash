#!/bin/sh
#
# Zabbix daemon start/stop script.
#
# Written by Alexei Vladishev <alexei.vladishev@zabbix.com>.
NAME=zabbix_proxy
PATH=/bin:/usr/bin:/sbin:/usr/sbin:/home/zabbix/bin
DAEMON=/usr/local/sbin/${NAME}
DESC="Zabbix server daemon"
PID=/tmp/$NAME.pid
test -f $DAEMON || exit 0
set -e
case "$1" in
 start)
 echo "Starting $DESC: $NAME"
 start-stop-daemon --oknodo --start --pidfile $PID \
 --exec $DAEMON
 ;;
 stop)
 echo "Stopping $DESC: $NAME"
 start-stop-daemon --oknodo --stop --pidfile $PID \
 --exec $DAEMON
 ;;
 restart|force-reload)
 $0 stop
 sleep 3
 $0 start
 ;;
 *)
 N=/etc/init.d/$NAME
 echo "Usage: $N {start|stop|restart|force-reload}" >&2
 exit 1
 ;;

esac
exit 0
