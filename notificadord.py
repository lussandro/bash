#!/usr/bin/python -u
# -*- coding: utf-8 -*-

__author__ = "Henrique Grolli Bassotto for OpenS Tecnologia"
__date__ = "Dec 2010"

import logging
import string
import os
import MySQLdb
import sys
import httplib
import urllib2
import urllib
from daemon import Daemon
from ConfigParser import ConfigParser
from _mysql_exceptions import OperationalError
import re
from twisted.internet import reactor, protocol
from optparse import OptionParser
from starpy import manager
# Fixing for debian
try:
    import json
except ImportError:
    import simplejson as json
LOG_PATH = '/var/log/snep/notificadord'

# Logging valid Values:
# DEBUG : Detailed information, typically of interest only when diagnosing problems.
# INFO : Confirmation that things are working as expected.
# WARNING : An indication that something unexpected happened, or indicative of some problem in the near future. 
# ERROR : Due to a more serious problem, the software has not been able to perform some function.
# CRITICAL : A serious error, indicating that the program itself may be unable to continue running.

logging.basicConfig(filename=LOG_PATH,level=logging.DEBUG)
logging.getLogger("").setLevel(logging.DEBUG)

amilog = logging.getLogger("AMI")
amilog.setLevel(logging.ERROR)

log = logging.getLogger("NOTIFICADOR")
log.setLevel(logging.DEBUG)

# -----------------------------------------------------------------------------------
#
# -----------------------------------------------------------------------------------
class MyAMIFactory(manager.AMIFactory):
    amiWorker = None

    def __init__(self, username, password, worker):
        log.debug("Inicializando MyAMIFactory...")
        self.amiWorker = worker
        manager.AMIFactory.__init__(self, username, password)

    def clientConnectionLost(self, connector, reason):
        log.error("Connection Lost, reason: %s" % reason.value)
        self.amiWorker.onLoseConnection(reason)
        reactor.callLater(10, self.amiWorker.connect)

    def clientConnectionFailed(self, connector, reason):
        log.error("Connection Lost, reason: %s" % reason.value)
        self.amiWorker.onLoseConnection(reason)
        reactor.callLater(10, self.amiWorker.connect)

# -----------------------------------------------------------------------------------
#
# -----------------------------------------------------------------------------------
class AMIWorker():
    connected = False
    amiFactory = None
    ami = None
    db = None

    clientsFactory = None
    serviceurl = None
    mode = "before"
    onlyagent = None
    transfer = None
    notifyagent = None
    ids = {}

    ami_host = None
    ami_port = None
    ami_user = None
    ami_pass = None

    # controle de repetição, buffer com 20 posições para controlar mensagens
    # duplicadas em evento Link
    linkbuffer = []

    def __init__(self, hostname='127.0.0.1', port=5038, username='snep', password='sneppass'):
        log.debug("Inicializando AMIWorker...")
        self.ami_host = hostname
        self.ami_port = port
        self.ami_user = username
        self.ami_pass = password
        self.handlers = {
            "Dial": self._handleDial
        }

        self.amiFactory = MyAMIFactory(self.ami_user, self.ami_pass, self)

        reactor.callLater(1, self.connect)

    def connect(self):
        log.debug("Tentando se conectar a %s:%s" % (self.ami_host, self.ami_port))
        d = self.amiFactory.login(self.ami_host, self.ami_port)
        d.addCallback(self.onLoginSuccess)
        d.addErrback(self.onLoginFailure)
        return d

    def setMode(self, mode):
        self.mode = mode
        if mode == "after":
            self.handlers = {
                "Link": self._handleLink,
                "UserEvent": self._handleUserfield

            }
        else:
            self.handlers = {
                "Dial": self._handleDial,
                "UserEvent": self._handleUserfield
            }

    def setServiceurl(self, serviceurl):
        if serviceurl != None:
            self.serviceurl = urllib2.Request(serviceurl)

    def setOnlyagent(self, onlyagent):
        self.onlyagent = onlyagent

    def setTransfer(self, transfer):
            self.transfer = transfer

    def setNotifyagent(self, notifyagent):
        self.notifyagent = notifyagent

    def onLoginSuccess(self, ami):
        log.info("Autenticacao bem sucedida...")
        self.ami = ami
        self.connected = True
        for event, handler in self.handlers.items():
            self.ami.registerEvent(event, handler)

    def onLoginFailure(self, reason):
        log.error("Falha de Autenticacao: %s" % reason.value)
        self.connected = False

    def onLoseConnection(self, reason):
        log.error("Perda de Conexao: %s" % reason.value)
        self.connected = False

    # -----------------------------------------------------------------------------------
    # Verifica no banco de dados qual o ramal do canal
    # -----------------------------------------------------------------------------------
    def resolvChannelOwner(self, channel):

        c = self.db.cursor()
        c.execute("SELECT name FROM peers WHERE canal='%s' AND peer_type='R'" % channel)
        data = c.fetchone()
        c.fetchall()
        c.close()
        if data != None:
            return data[0]
        else:
            return None
    # -----------------------------------------------------------------------------------
    # Verifica se tem agente logado no ramal
    # -----------------------------------------------------------------------------------
    def resolvLoggedAgent(self,channel):

        c = self.db.cursor()
        c.execute("SELECT agent FROM logged_agents WHERE extension='%s'" % channel)
        data = c.fetchone()
        c.fetchall()
        c.close()
        if data != None:
            return data[0]
        else:
            return None

    # -----------------------------------------------------------------------------------
    # Monta e transmite a requisição http via get
    # -----------------------------------------------------------------------------------
    def _notifyWebService(self, message):
        headers = {"Content-type": "application/json", "Accept": "application/json"}

        conn = httplib.HTTPConnection(self.serviceurl.get_host())

        url = self.serviceurl.get_selector()

        if self.method == "GET":
            params = urllib.urlencode(message)
            if url.find("?") != -1:
                url = url + "&" + params
            else:
                url = url + "?" + params

        params = json.dumps(message)
        log.debug("JSON send: %s" % params)

        conn.request(self.method, url, params, headers)
        response = conn.getresponse()
        data = response.read()
        log.debug("WebService response: %s" % data)
        conn.close()

    def _noCustomOpt(self, custom, args):
        self._handleNotify(None, args)

    # -----------------------------------------------------------------------------------
    # Popula um dicionario com o userfield de cada chamada que passa pelo notificador
    # -----------------------------------------------------------------------------------
    def setIds(self, uniqueid, userfield):
        log.debug('Uniqueid: %s UserField: %s' % (uniqueid, userfield))
        if not self.ids:
            self.ids = {uniqueid : userfield}
        else:
            self.ids[uniqueid] = userfield

    # -----------------------------------------------------------------------------------
    # Gera a notificação baseado nos parâmetros da Interface
    # -----------------------------------------------------------------------------------
    def _handleNotify(self, custom, args):

        log.debug("---------------------DEBUG handleNotify - Inicio ---------------------")
        # Se o valor de channel contiver a string "Local"  = Agente
        channel = args['channel']
        ch = channel[0:5] == "Local"
        # Redefine o valor de Extension
        ifagt=True
        if ch :                     ### é agente
            # Verifica se tem tem agente logado no Ramal - quando transferencia
            if channel.find("transferencias") > 0 :
                dst = args['destination'] 
                if dst.find("/") > 0:
                    dst = dst[dst.find("/")+1:]
                if dst.find("-") > 0:
                    dst = dst[:dst.find("-")]
                agt = str(self.resolvLoggedAgent(dst))
                log.debug("Agt quando transferencia:  %s" % agt) ;
                if agt != "None":
                    extension = channel
                else: 
                    extension = dst
                    ifagt = False
            else:
                extension = channel
        else:  ### Nao é agente, tenta resolver o canal
            extension = self.resolvChannelOwner(channel)
            ifagt = False

        log.debug("Custon = %s" % custom)
        log.debug("Args = %s" % args)
        log.debug("Extension = %s" % extension)
        log.debug("ch = %s " % ch)
        log.debug("OnlyAgent = %s" % self.onlyagent)
        log.debug("NotifyAgent = %s" % self.notifyagent)
        log.debug("Ifagt = %s" % ifagt)

        ## Controle de envio da notificação
        send_args = False

        if extension != None :
            args['exten'] = extension
            if custom != None and custom != "":
                custom = custom.replace("'", '"')
                try:
                    custom = json.loads(custom)
                except:
                    log.warn("Invalid custom args sent: '%s'" % custom)
                    custom = {}
                args.update(custom)
            else:
                if not self.onlyagent:
                    send_args = True
            if (ch and self.notifyagent) or (ch and not self.notifyagent and self.onlyagent)  :
                log.debug("Notifica como Agente: Origem-> Destino: [%s -> %s]" % (args['callerid'], extension))
                if ifagt:
                    agente = extension.split('/')
                    agt = str(agente[1])
                    if agt.find("@") >= 0: 
                        args['exten'] = "Agente/" + agt[:agt.find("@")]
                    else:
                        args['exten'] = "Agente/" + agt
                    send_args = True
                else:
                    if self.onlyagent:
                        send_args = False
                    else: 
                        send_args = True
            else:
                if not self.onlyagent and not self.notifyagent:
                    log.debug("Notifica como Ramal: Origem->Destino: [%s -> %s]" % (args['callerid'], extension))
                    send_args = True
        else:
            if not self.onlyagent and not self.notifyagent:
                log.debug("Extension desconhecida: Origem->Destino: [%s -> %s]" % (args['callerid'], extension))


        log.debug("Args = %s" % args)
        log.debug("Send_Args para porta ou url ? %s" % send_args)
        try:
            del(args['destination']) ; 
        except KeyError:
            # Destination do not exist
            pass

        if send_args: 
            # Notificacoes para a URL, se existir uma configurada na interface
            if self.serviceurl != None:
                self._notifyWebService(args)
            # Notificacoes para a porta 60000
            self.clientsFactory.sendMessageToAllClients(json.dumps(args))
        else:
            log.info("Nenhuma notificacao enviada.")

        log.debug("---------------------DEBUG handleNotify - Fim ---------------------")

    # -----------------------------------------------------------------------------------
    # HandleDial - Notifica quando: mode = before
    # -----------------------------------------------------------------------------------
    def _handleDial(self, ami, event):
        d = ami.getVar(event['source'], 'CUSTOM_OPT')
        log.debug("---------------------DEBUG handleDial Inicio ---------------------")
        log.debug(event) ;


        # verifica se ligacao é uma transferência
        # transfer = Valor de notificação de transferencia. 0 nao notifica transferência
        if not self.transfer:
            try:
                canal = event['source']
                #pos < 0 nao é transferência
                pos = canal.find('transfer')

                if pos > 0:
                    flag = 0
                else:
                    flag = 1
            except KeyError:
                flag = 1
        else:
            flag = 1

        log.debug("Flag transferência: %s" % flag)

        if flag > 0:
            if event['destination'].find("-") != -1:
                channel = string.replace(event['destination'][0:event['destination'].find("-")], '"', '')
                log.debug("Channel :  %s" % channel) ;
                if self.notifyagent:
                    agt = str(self.resolvLoggedAgent(channel[channel.find('/')+1:]))
                    log.debug("Agt :  %s" % agt) ;
                    if agt != "None":
                        channel = 'Local/'+agt
            else:
                channel = event['destination']

            log.debug("channel= %s" % channel )
            log.debug("notifyagent= %s" % self.notifyagent)

            if self.notifyagent or (self.onlyagent and not self.notifyagent):
                source = event['source']
                ch = source[0:5] == "Local"
                if ch:
                    channel = source[0:source.find('agent')-1]
                else:
                    #if not self.onlyagent:
                    agt = str(self.resolvLoggedAgent(channel[channel.find('/')+1:]))
                    if agt != "None":
                        channel = 'Local/'+agt
            try: 
                ui=event['srcuniqueid'];
                unqid=self.ids[ui]
            except Exception,E:
                log.debug("Erro na leitura do uniqueid/userfield: %s" % E)
                log.debug("Dicionario de ID's:  %s" % self.ids) 
                unqid=""

            args = {"destination": event['destination'], "channel": channel, "callerid": event['callerid'], "calleridname": event['calleridname'], "userfield" : unqid}
            log.debug("Args send to notify: %s" % args)
            # Remove da lista de userfields, o userfield ja usado
            if unqid != "":
                try:
                    del(self.ids[ui]) 
                except Exception, E:
                    log.debug("Erro ao apagar ID: %s " % ui[:ui.find(".")])
                    log.debug("Dicionario de ID's:  %s" % self.ids)
            d.addCallback(self._handleNotify, args)
            d.addErrback(self._noCustomOpt, args)

        log.debug("---------------------DEBUG handleDial FIM ---------------------")

        return d

    # -----------------------------------------------------------------------------------
    # HandleLink - Notifica quando: mode = after
    # -----------------------------------------------------------------------------------
    def _handleLink(self, ami, event):
        linkId = None
        try: 
            linkId=event['uniqueid1'];
        except Exception,E:
            pass

        log.debug("---------------------DEBUG handleLink Inicio ---------------------")
        log.debug(event) ;

        # verifica se ligacao é uma transferência
        # transfer = Valor de notificação de transferencia. 0 nao notifica transferência
        if not self.transfer:
            try:
                canal = event['channel1']
                #pos < 0 nao é transferência
                pos = canal.find('transfer')

                if pos > 0:
                    flag = 0
                else:
                    flag = 1
            except KeyError:
                flag = 1
        else:
            flag = 1

        log.debug("Flag transferência: %s" % flag)

        if flag > 0:
            # verificando/ignorando eventos já em buffer
            if event['uniqueid2'] not in self.linkbuffer:
                self.linkbuffer.append(event['uniqueid2'])

                # removendo mais antigo conforme nosso buffer chega em 50 entradas
                if len(self.linkbuffer) > 50:
                    self.linkbuffer.pop(0)

                if event['channel2'].find("-") != -1:
                    channel = string.replace(event['channel2'][0:event['channel2'].find("-")], '"', '')
                    log.debug("Channel :  %s" % channel) ;
                    if self.notifyagent or self.onlyagent:
                        agt = str(self.resolvLoggedAgent(channel[channel.find('/')+1:]))
                        log.debug("Agt :  %s" % agt) ;
                        if agt != "None":
                            channel = 'Local/'+agt
                else:
                    channel = event['channel2']
                try: 
                    ui=event['uniqueid1'];
                    unqid=self.ids[ui]
                except Exception,E:
                    log.debug("Erro na leitura de uniqueid/userfield: %s"% E)
                    log.debug("Dicionario de ID's:  %s" % self.ids) 
                    log.debug(event)
                    unqid=""
                    return

                args = {"destination": event['callerid2'], "channel": channel, "callerid": event['callerid1'], "calleridname": event['callerid1'], "userfield": unqid}
                log.debug("Args send to notify: %s" % args)
                # Remove da lista de userfields, o userfield ja usado
                if unqid != "":
                    try:
                        del(self.ids[ui])
                    except Exception, E:
                        log.debug("Erro ao apagar ID: %s " % ui[:ui.find(".")])
                        log.debug("Dicionario de ID's:  %s" % self.ids)


                d = ami.getVar(event.get('channel1'), 'CUSTOM_OPT')
                d.addCallback(self._handleNotify, args)
                d.addErrback(self._noCustomOpt, args)

                log.debug("---------------------DEBUG handleLink FIM ---------------------")
                return d

    # -----------------------------------------------------------------------------------
    # HandleUserfield - Captura dos eventos o Userfield
    # -----------------------------------------------------------------------------------
    def _handleUserfield(self, ami, event):
        if (event['userevent'] != "SnepUserfield" ):
            return

        log.debug("---------------------DEBUG handleUserfield Inicio ---------------------")
        log.debug("UserEvent SnepUserfield: %s" % event) ;
        
        self.setIds(event['uniqueid'], event['userfield']) 
        
        log.debug("---------------------DEBUG handleUserfield FIM ---------------------")

    def setGetMethod(self, method):
        self.method = method

# -----------------------------------------------------------------------------------
# Classe para controlar a comunicação do notificador via TCP
# -----------------------------------------------------------------------------------
class TCPProtocol(protocol.Protocol):
    """Protocolo de comunicação via TCP para o daemon Notificador"""

    def connectionMade(self):
        print("Adicionando listener: %s" % self.transport.client[0])
        self.factory.clientProtocols.append(self)

    def sendLine(self, line):
        self.transport.write(line + "\r\n")

    def connectionLost(self, reason):
        print("Removendo listener: %s" % self.transport.client[0])
        self.factory.clientProtocols.remove(self)

# -----------------------------------------------------------------------------------
#
# -----------------------------------------------------------------------------------
class ProtocolFactory(protocol.ServerFactory):
    protocol = TCPProtocol

    def __init__(self):
        self.clientProtocols = []

    def sendMessageToAllClients(self, mesg):
        for client in self.clientProtocols:
            client.sendLine(mesg)

# -----------------------------------------------------------------------------------
# Trata das Conexões com o banco de Dados
# -----------------------------------------------------------------------------------
class DB:
    conn = None

    user = ""
    passwd = ""
    db = ""

    def __init__(self, user, passwd, db):
        self.user = user
        self.passwd = passwd
        self.db = db

    def connect(self):
        self.conn = MySQLdb.connect(user=self.user, passwd=self.passwd, db=self.db)
        self.conn.autocommit(True)


    def cursor(self):
        try:
            cursor = self.conn.cursor()
            # ridiculous error check for terrible python dbapi
            cursor.execute("SELECT 1")
            cursor.fetchall()
            return cursor
        except (AttributeError, OperationalError):
            self.connect()
            return self.conn.cursor()

def run_reactor():

    os.chdir(os.path.dirname(__file__))

    config = ConfigParser()

    # assumindo que estamos em modules/notificador/bin
    config.read('../../../includes/setup.conf')

    def cfg(section, key):
        return string.replace(config.get(section, key), '"', '')

    # pega configurações para acessar BD e obtem parâmetros
    db = DB(cfg('ambiente', 'db.username'), cfg('ambiente', 'db.password'), cfg('ambiente', 'db.dbname'))

    a = db.cursor()
    a.execute("SELECT value FROM registry WHERE context='notificador' AND `key`='mode'")
    mode = a.fetchone()
    if mode != None:
        mode = mode[0]
    else:
        mode = "before"
    a.fetchall()
    a.close()

    b = db.cursor()
    b.execute("SELECT value FROM registry WHERE context='notificador' AND `key`='serviceurl'")
    serviceurl = b.fetchone()
    if serviceurl != None:
        serviceurl = serviceurl[0]
    else:
        serviceurl = None
    b.fetchall()
    b.close()

    c = db.cursor()
    c.execute("SELECT value FROM registry WHERE context='notificador' AND `key`='transfer'")
    transfer = c.fetchone()
    if transfer != None:
        transfer = bool(int(transfer[0]))
    else:
        transfer = None 
    c.fetchall()
    c.close()

    d = db.cursor()
    d.execute("SELECT value from registry WHERE context='notificador' AND `key`='notifyagent'")
    notifyagent = d.fetchone()
    if notifyagent != None:
        notifyagent = bool(int(notifyagent[0]))
    else:
        notifyagent = None
    d.fetchall()
    d.close()

    e = db.cursor()
    e.execute("SELECT value from registry WHERE context='notificador' AND `key`='onlyagent'")
    onlyagent = e.fetchone()
    if onlyagent != None:
        onlyagent = bool(int(onlyagent[0]))
    else:
        onlyagent = None
    e.fetchall()
    e.close()

    f = db.cursor()
    f.execute("SELECT value from registry WHERE context='notificador' AND `key`='method'")
    method = f.fetchone()
    if method != None:
        method = method[0]
    else:
        method = "GET"
    f.fetchall()
    f.close()

    param = {'Mode=': mode, 'Method=': method, 'OnlyAgent=': onlyagent, 'NotifyAgent=': notifyagent, 'Transfer=': transfer, 'ServiceUrl=': serviceurl}
    log.info(param) ;

    ami = AMIWorker(cfg('ambiente', 'ip_sock'), 5038, cfg('ambiente', 'user_sock'), cfg('ambiente', 'pass_sock'))
    ami.setMode(mode)
    ami.setGetMethod(method)
    ami.setServiceurl(serviceurl)
    ami.setNotifyagent(notifyagent)
    ami.setOnlyagent(onlyagent)
    ami.setTransfer(transfer)
    ami.db = db

    factory = ProtocolFactory()
    ami.clientsFactory = factory
    reactor.listenTCP(60000, factory)

    log.info("Listen at 0.0.0.0:60000")
    log.info("Notificador started")
    reactor.run()
# -----------------------------------------------------------------------------------
#
# -----------------------------------------------------------------------------------
class NotificadorServerDaemon(Daemon):
    def run(self):
        run_reactor()

if __name__ == '__main__':

    daemon = NotificadorServerDaemon('/var/run/notificador.pid')
    

    if len(sys.argv) == 2:
        if 'start' == sys.argv[1]:
            daemon.start()
        elif 'stop' == sys.argv[1]:
            daemon.stop()
        elif 'restart':
            daemon.restart()
        else:
            print "Unknown command"
            sys.exit(2)
        sys.exit(0)
    else:
        print "usage: %s start|stop|restart" % sys.argv[0]
        sys.exit(2)
