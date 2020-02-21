# -*- coding: utf-8 -*-
import os, sys
import hmac
import time
import base64
import asyncio
import hashlib
import rsa
import requests
from . import config
from . import log
from .models import SyncAsync, ApplicationHeader
from .connections import Connection
from .lib.Gen.ttypes import *

logs = log.LOGGER

class Auth(Connection):
	def __init__(self, client):
		super().__init__(config.MAIN_PATH)
		self.cli = client
		self.LA, self.UA = self.cli.LA, self.cli.UA
		self.updateHeaders({
			'User-Agent': self.UA,
			'X-Line-Application': self.LA,
			'X-Line-Carrier': config.CARRIER,
			"x-lal":"in_ID"
		})
		self.afterLoginRemote = []

	def remote(self, *func):
		self.afterLoginRemote.extend(func)
		
	#crypto
	def __write_val(self, data):
		return (chr(len(data)) + data)

	def __gen_message(self, tuple_msg):
		return (''.join(tuple_msg)).encode('utf-8')

	def __rsa_crypt(self, message,RSA):
		pub_key = rsa.PublicKey(int(RSA.nvalue, 16), int(RSA.evalue, 16))
		crypto  = rsa.encrypt(message, pub_key)
		return crypto

	def _encryptedEmailAndPassword(self, mail, passwd, RSA):
		message_ = (
			self.__write_val(RSA.sessionKey),
			self.__write_val(mail),
			self.__write_val(passwd),
		)
		message = self.__gen_message(message_)
		crypto  = self.__rsa_crypt(message, RSA).hex()
		return crypto

	def _encryptedPassword(self, phone, password, RSA):
		message_ = (
			self.__write_val(RSA.sessionKey),
			self.__write_val(phone),
			self.__write_val(passwd),
		)
		message = self.__gen_message(message_)
		crypto  = self.__rsa_crypt(message, RSA).hex()
		return crypto

	def waitForPhoneConfirm(self, verifier):
		r = requests.get(config.BASE_URL + config.WAIT_FOR_MOBILE_PATH, headers={
			'X-Line-Access': verifier
		})
		return r
			
	async def createLoginSession(self, name, token, mail, passwd, certt, qr):
		if token is not None:
			await self.loginWithAuthToken(token)
		elif mail and passwd is not None:
				pname = name if name else mail
				pname += ".session"
				if os.path.exists(pname):
					y = open(pname, "r").read().strip()
					await self.loginWithCredential(mail=mail, password=passwd, cert=y)
				else:
					await self.loginWithCredential(mail=mail, password=passwd,
											path = pname)
		elif mail and passwd and cert is not None:
			await self.loginWithCredential(mail=mail, password=passwd, cert=certt)
		elif qr and name is not None:
			if (name is not None and os.path.exists(name+'.session')):
				token = open(name+'.session', "r").read()
				await self.loginWithAuthToken(token.strip())
			else:
				await self.loginWithQrcode(path=name+".session" if name else None)
		else:
			raise ValueError("Must pass once paramater for login")
		logs.info("Login success as %s" % (self.profile.displayName))
		return True
				
	async def loginWithQrcode(self, path=None):
		self.url(config.MAIN_PATH)
		qr = await self.call('getAuthQrcode', True, "AsyncLine", "")
		print("line://au/q/"+qr.verifier)
		r = self.waitForPhoneConfirm(qr.verifier)
		vr = r.json()['result']['verifier']
		self.url(config.AUTH_PATH)
		rq = LoginRequest(
			LoginType.QRCODE,
			IdentityProvider.LINE,
			None,
			None,
			True,
			config.LOGIN_LOCATION,
			"AsyncLine",
			None,
			vr,
			None,
			2
		)
		lr = await self.call('loginZ', rq)
		self.updateHeaders({
			'X-Line-Access': lr.authToken
		})
		self.authToken = lr.authToken
		self.cert = lr.certificate
		if path:
			with open(path, "w") as fp:
				fp.write(lr.authToken)
		await self.afterLogin()

	async def loginWithCredential(self, mail, password, cert=None, path=None):
		self.url(config.MAIN_PATH)
		rsakey = await self.call('getRSAKeyInfo', config.LOGIN_PROVIDER)
		crypt  = self._encryptedEmailAndPassword(mail, password, rsakey)
		self.url(config.AUTH_PATH)
		rq = LoginRequest(
			LoginType.ID_CREDENTIAL,
			IdentityProvider.LINE_PHONE,
			rsakey.keynm,
			crypt,
			True,
			config.LOGIN_LOCATION,
			"AsyncLine",
			cert,
			None,
			crypt.encode() if type(crypt) == str else crypt, #none, #crypt
			0
		)
		result = await self.call('loginZ', rq)
		self.url(config.MAIN_PATH)
		if result.type == 3:
			print("Please confirm this code on your device %s"% (result.pinCode))
			r = self.waitForPhoneConfirm(result.verifier)
			rq = LoginRequest(
				LoginType.QRCODE,
				IdentityProvider.LINE,
				None, None, True,
				config.LOGIN_LOCATION,
				self.LA.split('\t')[0],
				cert, r.json()['result']['verifier'],
				None, 
				2
			)
			self.url(config.AUTH_PATH)
			result = await self.call('loginZ', rq)
			self.updateHeaders({
				'X-Line-Access': result.authToken,
			})
			self.authToken = result.authToken
			self.cert = result.certificate
			self.url(config.MAIN_PATH)
		elif result.type == 1:
			self.authToken = result.authToken
			self.cert = result.certificate
			self.updateHeaders({
				'X-Line-Access': result.authToken
			})
		else:
			logs.critical('Login failed. got result type `%s`' % (result.type))
			
		if path is not None:
			with open(path, "w") as fp:
				fp.write(self.cert)
		await self.afterLogin()
	
	async def loginWithAuthToken(self, authToken, path=None):
		self.url(config.MAIN_PATH)
		self.updateHeaders({
			'X-Line-Access': authToken
		})
		self.authToken = authToken
		await self.afterLogin()

	async def afterLogin(self):
		self.url(config.NORMAL_PATH)
		self.profile = await self.call('getProfile')
		self.last_rev = await self.call('getLastOpRevision')
		self.settings = await self.call('getSettings')
		self.authToken = self.authToken
		
		for remoteFunc in self.afterLoginRemote:
			remoteFunc(**{
				'profile': self.profile,
				'settings': self.settings,
				'rev': self.last_rev,
				'mid': self.profile.mid,
				'authToken': self.authToken,
				'cert': getattr(self, 'cert', None),
				'app_header': (self.LA, self.UA),
			})

	async def logout(self):
		await self.call("logoutZ")