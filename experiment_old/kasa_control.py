import asyncio
from kasa import Discover

class Kasa_Device:
    def __init__(self, ip, username="", password=""):
        self.ip = ip
        self.username = username
        self.password = password
        self.dev = None

    async def connect(self):
        dev = await Discover.discover_single(self.ip, username=self.username, password=self.password)
        await dev.update()
        self.dev = dev

    async def turn_on(self):
        await self.dev.turn_on()
        await self.dev.update()

    async def turn_off(self):
        await self.dev.turn_off()
        await self.dev.update()

if __name__ == "__main__":
    ip = "192.0.2.123"
    username = ""
    password = ""
    kasa_device = Kasa_Device(ip, username, password)
    asyncio.run(kasa_device.connect())
    asyncio.run(kasa_device.turn_on())