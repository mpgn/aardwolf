
import pyperclip
from aardwolf.extensions.RDPECLIP.protocol.formatlist import CLIPRDR_LONG_FORMAT_NAME
import asyncio
import traceback
import enum

from aardwolf import logger
from aardwolf.commons.iosettings import RDPIOSettings
from aardwolf.channels import Channel
from aardwolf.protocol.T124.userdata.constants import ChannelOption
from aardwolf.extensions.RDPECLIP.protocol import *
from aardwolf.extensions.RDPECLIP.protocol.clipboardcapabilities import CLIPRDR_GENERAL_CAPABILITY, CB_GENERAL_FALGS
from aardwolf.protocol.channelpdu import CHANNEL_PDU_HEADER, CHANNEL_FLAG
from aardwolf.extensions.RDPECLIP.protocol.formatlist import CLIPBRD_FORMAT,CLIPRDR_SHORT_FORMAT_NAME, CLIPRDR_LONG_FORMAT_NAME
from aardwolf.commons.queuedata import *
from aardwolf.commons.queuedata.clipboard import RDP_CLIPBOARD_DATA_TXT
from aardwolf.protocol.T128.security import TS_SECURITY_HEADER,SEC_HDR_FLAG, TS_SECURITY_HEADER1

class CLIPBRDSTATUS(enum.Enum):
	WAITING_SERVER_INIT = enum.auto()
	CLIENT_INIT = enum.auto()
	RUNNING = enum.auto()


class RDPECLIPChannel(Channel):
	name = 'cliprdr'
	def __init__(self, iosettings:RDPIOSettings):
		Channel.__init__(self, self.name, ChannelOption.INITIALIZED|ChannelOption.ENCRYPT_RDP|ChannelOption.COMPRESS_RDP|ChannelOption.SHOW_PROTOCOL)
		self.use_pyperclip = iosettings.clipboard_use_pyperclip
		self.status = CLIPBRDSTATUS.WAITING_SERVER_INIT
		self.compression_needed = False #TODO: tie it to flags
		self.channel_data_out_q = asyncio.Queue()
		self.channel_data_monitor_task = None
		self.supported_formats = [CLIPBRD_FORMAT.CF_UNICODETEXT] #, CLIPBRD_FORMAT.CF_HDROP
		self.server_caps = None
		self.server_general_caps = None
		self.client_general_caps_flags = CB_GENERAL_FALGS.HUGE_FILE_SUPPORT_ENABLED | CB_GENERAL_FALGS.FILECLIP_NO_FILE_PATHS | CB_GENERAL_FALGS.STREAM_FILECLIP_ENABLED #| CB_GENERAL_FALGS.USE_LONG_FORMAT_NAMES # CB_GENERAL_FALGS.CAN_LOCK_CLIPDATA | #| CB_GENERAL_FALGS.USE_LONG_FORMAT_NAMES
		self.__buffer = b''
		self.current_server_formats = {}
		self.__requested_format = None
		self.__current_clipboard_data:RDP_CLIPBOARD_DATA_TXT = None 

	async def start(self):
		try:
			if self.use_pyperclip is True:
				try:
					import pyperclip
				except ImportError:
					print('Could not import pyperclip! Copy-paste will not work!')
				
				else:
					if not pyperclip.is_available():
						print("pyperclip - Copy functionality available!")


			self.channel_data_monitor_task = asyncio.create_task(self.channel_data_monitor())
			#self.process_msg_in_task = asyncio.create_task(self.process_msg_in_task())
			return True, None
		except Exception as e:
			return None, e

	async def channel_data_monitor(self):
		try:
			while True:
				data = await self.channel_data_out_q.get()
				if len(data) < 16000:
					if self.compression_needed is False:
						flags = CHANNEL_FLAG.CHANNEL_FLAG_FIRST|CHANNEL_FLAG.CHANNEL_FLAG_LAST|CHANNEL_FLAG.CHANNEL_FLAG_SHOW_PROTOCOL
						packet = CHANNEL_PDU_HEADER.serialize_packet(flags, data)

					else:
						raise NotImplementedError('Compression not implemented!')
				else:
					raise NotImplementedError('Chunked send not implemented!')

				sec_hdr = None
				if self.connection.cryptolayer is not None:
					sec_hdr = TS_SECURITY_HEADER()
					sec_hdr.flags = SEC_HDR_FLAG.ENCRYPT
					sec_hdr.flagsHi = 0

				await self.connection.handle_out_data(packet, sec_hdr, None, None, self.channel_id, False)

			return True, False
		except Exception as e:
			traceback.print_exc()
			return None,e

	async def __send_capabilities(self):
		# server sent monitor ready, now we must send our capabilites
		try:
			# sending capabilities
			gencap = CLIPRDR_GENERAL_CAPABILITY()
			gencap.generalFlags = self.client_general_caps_flags

			caps = CLIPRDR_CAPS()
			caps.capabilitySets.append(gencap)

			msg = CLIPRDR_HEADER.serialize_packet(CB_TYPE.CB_CLIP_CAPS, 0, caps)
			await self.channel_data_out_q.put(msg)

			## if remote drive is attached this should be sent
			# sending tempdir location
			# tempdir = CLIPRDR_TEMP_DIRECTORY()
			# tempdir.wszTempDir = 'C:\\Windows\\Temp\\'
			# msg = CLIPRDR_HEADER.serialize_packet(CB_TYPE.CB_TEMP_DIRECTORY, 0, tempdir)
			# await self.channel_data_out_q.put(msg)

			# synchronizing formatlist
			fmtl = CLIPRDR_FORMAT_LIST()
			for reqfmt in self.supported_formats:
				fe = CLIPRDR_LONG_FORMAT_NAME()
				fe.formatId = reqfmt
				fmtl.templist.append(fe)

			self.status = CLIPBRDSTATUS.CLIENT_INIT
			msg = CLIPRDR_HEADER.serialize_packet(CB_TYPE.CB_FORMAT_LIST, 0, fmtl)
			await self.channel_data_out_q.put(msg)
		
			return True, None
		except Exception as e:
			return None, e
	
	async def __process_in(self):
		try:
			hdr = CLIPRDR_HEADER.from_bytes(self.__buffer)

			if self.status == CLIPBRDSTATUS.RUNNING:
				if hdr.msgType == CB_TYPE.CB_FORMAT_LIST:
					fmtl = CLIPRDR_FORMAT_LIST.from_bytes(self.__buffer[8:8+hdr.dataLen], longnames=CB_GENERAL_FALGS.USE_LONG_FORMAT_NAMES in self.client_general_caps_flags, encoding='ascii' if CB_FLAG.CB_ASCII_NAMES in hdr.msgFlags else 'utf-16-le')
					self.current_server_formats = {}
					for fmte in fmtl.templist:
						self.current_server_formats[fmte.formatId] = fmte
					
					#print(self.current_server_formats)
					# sending back an OK
					msg = CLIPRDR_HEADER.serialize_packet(CB_TYPE.CB_FORMAT_LIST_RESPONSE, CB_FLAG.CB_RESPONSE_OK, None)
					await self.channel_data_out_q.put(msg)

					if self.use_pyperclip is True and CLIPBRD_FORMAT.CF_UNICODETEXT in self.current_server_formats.keys():
						# pyperclip is in use and server just notified us about a new text copied, so we request the text
						# automatically
						self.__requested_format = CLIPBRD_FORMAT.CF_UNICODETEXT
						dreq = CLIPRDR_FORMAT_DATA_REQUEST()
						dreq.requestedFormatId = CLIPBRD_FORMAT.CF_UNICODETEXT
						msg = CLIPRDR_HEADER.serialize_packet(CB_TYPE.CB_FORMAT_DATA_REQUEST, 0, dreq)
						await self.channel_data_out_q.put(msg)
				
				elif hdr.msgType == CB_TYPE.CB_FORMAT_DATA_RESPONSE:
					if hdr.msgFlags != hdr.msgFlags.CB_RESPONSE_OK:
						print('Server rejected our copy request!')
					else:
						try:
							fmtdata = CLIPRDR_FORMAT_DATA_RESPONSE.from_bytes(self.__buffer[8:8+hdr.dataLen],otype=self.__requested_format)
						
							if self.use_pyperclip is True and self.__requested_format in [CLIPBRD_FORMAT.CF_TEXT, CLIPBRD_FORMAT.CF_UNICODETEXT]:
								pyperclip.copy(fmtdata.dataobj)
						
						except Exception as e:
							raise e
						finally:
							self.__requested_format = None
				
				elif hdr.msgType == CB_TYPE.CB_FORMAT_DATA_REQUEST:

					fmtr = CLIPRDR_FORMAT_DATA_REQUEST.from_bytes(self.__buffer[8:8+hdr.dataLen])
					#print(fmtr)
					if fmtr.requestedFormatId == self.__current_clipboard_data.datatype:
						resp = CLIPRDR_FORMAT_DATA_RESPONSE()
						resp.dataobj = self.__current_clipboard_data.data
						resp = resp.to_bytes(self.__current_clipboard_data.datatype)

						msg = CLIPRDR_HEADER.serialize_packet(CB_TYPE.CB_FORMAT_DATA_RESPONSE, CB_FLAG.CB_RESPONSE_OK, resp)
						await self.channel_data_out_q.put(msg)
					
					else:
						print('Server requested a formatid which we dont have. %s' % fmtr.requestedFormatId)
						
			
			elif self.status == CLIPBRDSTATUS.WAITING_SERVER_INIT:
				# we expect either CLIPRDR_CAPS or CLIPRDR_MONITOR_READY
				if hdr.msgType == CB_TYPE.CB_CLIP_CAPS:
					self.server_caps = CLIPRDR_CAPS.from_bytes(self.__buffer[8:8+hdr.dataLen])
					self.server_general_caps = self.server_caps.capabilitySets[0] #it's always the generalflags
					logger.debug(self.server_general_caps)
				elif hdr.msgType == CB_TYPE.CB_MONITOR_READY:
					_, err = await self.__send_capabilities()
					if err is not None:
						raise err
				else:
					raise Exception('Unexpected packet type %s arrived!' % hdr.msgType.name)

				#await self.out_queue.put((data, err))
			elif self.status == CLIPBRDSTATUS.CLIENT_INIT:
				# initialization started, we already sent all necessary data
				# here we expect CB_FORMAT_LIST_RESPONSE
				if hdr.msgType == CB_TYPE.CB_FORMAT_LIST_RESPONSE:
					#this doesnt hold any data
					if CB_FLAG.CB_RESPONSE_OK in hdr.msgFlags:
						# everything was okay, now we can communicate on this channel normally
						# also we have to notify the client that they can use the keyboard now
						self.status = CLIPBRDSTATUS.RUNNING
						msg = RDP_CLIPBOARD_READY()
						await self.connection.ext_out_queue.put(msg)

					elif CB_FLAG.CB_RESPONSE_FAIL in hdr.msgFlags:
						raise Exception('Server refused clipboard initialization!')
					else:
						raise Exception('Server sent unexpected data! %s' % hdr)
				


			self.__buffer = self.__buffer[8+hdr.dataLen:]
			return True, None
		except Exception as e:
			return None, e

	async def monitor_in(self):
		try:
			while True:
				data, err = await self.raw_in_queue.get()
				if err is not None:
					await self.out_queue.put((data, err))
					raise err
				#print('Channel data in! "%s(%s)" <- %s' % (self.name, self.channel_id, data))		
				
				channeldata = CHANNEL_PDU_HEADER.from_bytes(data)
				#print('channeldata %s' % channeldata)
				self.__buffer += channeldata.data
				if CHANNEL_FLAG.CHANNEL_FLAG_LAST in channeldata.flags:
					_, err = await self.__process_in()
					if err is not None:
						raise err

		except asyncio.CancelledError:
			return None, None
		except Exception as e:
			traceback.print_exc()
			return None, e
	
	async def monitor_out(self):
		try:
			while True:
				data = await self.in_queue.get()
				#print('monitor out! %s' % data)
				if data.type == RDPDATATYPE.CLIPBOARD_DATA_TXT:
					# data in, informing the server that our clipboard has changed
					if data == self.__current_clipboard_data:
						#print('Data already in cache!')
						continue
					
					fmtl = CLIPRDR_FORMAT_LIST()
					for fmtid in [CLIPBRD_FORMAT.CF_UNICODETEXT]: #CLIPBRD_FORMAT.CF_TEXT, CLIPBRD_FORMAT.CF_OEMTEXT
						if CB_GENERAL_FALGS.USE_LONG_FORMAT_NAMES not in self.server_general_caps.generalFlags : #self.client_general_caps_flags:
							name = CLIPRDR_LONG_FORMAT_NAME()
							name.formatId = data.datatype
						else:
							name = CLIPRDR_SHORT_FORMAT_NAME()
							name.formatId = data.datatype
						fmtl.templist.append(name)
					msg = CLIPRDR_HEADER.serialize_packet(CB_TYPE.CB_FORMAT_LIST, 0, fmtl)
					await self.channel_data_out_q.put(msg)

					self.__current_clipboard_data = data
				
				else:
					print('Unhandled data type in! %s' % data.type)
					continue


		except asyncio.CancelledError:
			return None, None

		except Exception as e:
			traceback.print_exc()
			return None, e