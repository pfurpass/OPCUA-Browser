"""Kleiner OPC-UA-Server, der eine Anlage simuliert – zum Ausprobieren des Browsers."""

import asyncio
import logging
import math
import os
import random
import time

from asyncua import Server, ua, uamethod

ENDPOINT = os.environ.get("DEMO_ENDPOINT", "opc.tcp://0.0.0.0:4840/anlage/")
URI = "http://beispiel.local/anlage"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
log = logging.getLogger("demo-server")


async def main() -> None:
    server = Server()
    await server.init()
    server.set_endpoint(ENDPOINT)
    server.set_server_name("Demo-Anlage")
    server.set_security_policy([ua.SecurityPolicyType.NoSecurity])
    idx = await server.register_namespace(URI)

    plant = await server.nodes.objects.add_folder(idx, "Anlage")

    line = await plant.add_object(idx, "Linie1")
    oven = await line.add_object(idx, "Ofen")
    temperature = await oven.add_variable(idx, "Temperatur", 21.0, ua.VariantType.Double)
    setpoint = await oven.add_variable(idx, "Solltemperatur", 180.0, ua.VariantType.Double)
    heating = await oven.add_variable(idx, "Heizung_ein", False, ua.VariantType.Boolean)
    await setpoint.set_writable()
    await heating.set_writable()

    motor = await line.add_object(idx, "Foerderband")
    speed = await motor.add_variable(idx, "Drehzahl", 0, ua.VariantType.Int32)
    running = await motor.add_variable(idx, "Laeuft", False, ua.VariantType.Boolean)
    hours = await motor.add_variable(idx, "Betriebsstunden", 1284.5, ua.VariantType.Double)
    await speed.set_writable()
    await running.set_writable()
    await motor.add_property(idx, "Hersteller", "Musterwerk GmbH")

    tank = await line.add_object(idx, "Tank")
    level = await tank.add_variable(idx, "Fuellstand", 42.0, ua.VariantType.Float)
    pressure = await tank.add_variable(idx, "Druck", 1.013, ua.VariantType.Double)
    valves = await tank.add_variable(idx, "Ventile", [False, True, False, False], ua.VariantType.Boolean)
    await valves.set_writable()

    diag = await plant.add_object(idx, "Diagnose")
    counter = await diag.add_variable(idx, "Teilezaehler", 0, ua.VariantType.UInt32)
    state = await diag.add_variable(idx, "Betriebsart", "Automatik", ua.VariantType.String)
    fault = await diag.add_variable(idx, "Stoerung", False, ua.VariantType.Boolean)
    last_error = await diag.add_variable(idx, "LetzteMeldung", "keine", ua.VariantType.String)
    await state.set_writable()

    @uamethod
    def quittieren(parent, grund: str) -> bool:
        log.info("Störung quittiert: %s", grund)
        return True

    reason_arg = ua.Argument(
        Name="Grund",
        DataType=ua.NodeId(ua.ObjectIds.String),
        ValueRank=-1,
        Description=ua.LocalizedText("Warum die Störung quittiert wird"),
    )
    ok_arg = ua.Argument(
        Name="Erfolgreich",
        DataType=ua.NodeId(ua.ObjectIds.Boolean),
        ValueRank=-1,
        Description=ua.LocalizedText("Wahr, wenn die Meldung zurückgesetzt wurde"),
    )
    await diag.add_method(idx, "StoerungQuittieren", quittieren, [reason_arg], [ok_arg])

    log.info("Demo-Anlage lauscht auf %s", ENDPOINT)
    async with server:
        tick = 0
        while True:
            await asyncio.sleep(1)
            tick += 1
            t = time.time()
            await temperature.write_value(
                round(20 + 160 * (0.5 + 0.5 * math.sin(t / 30)) + random.uniform(-0.4, 0.4), 2),
                ua.VariantType.Double,
            )
            await level.write_value(round(40 + 30 * math.sin(t / 45), 2), ua.VariantType.Float)
            await pressure.write_value(
                round(1.0 + 0.4 * math.sin(t / 17) + random.uniform(-0.01, 0.01), 3), ua.VariantType.Double
            )
            if await running.read_value():
                await speed.write_value(1400 + random.randint(-20, 20), ua.VariantType.Int32)
                await counter.write_value(tick * 3, ua.VariantType.UInt32)
                await hours.write_value(round(1284.5 + tick / 3600, 4), ua.VariantType.Double)
            else:
                await speed.write_value(0, ua.VariantType.Int32)
            if tick % 60 == 0:
                broken = not await fault.read_value()
                await fault.write_value(broken, ua.VariantType.Boolean)
                await last_error.write_value("Türkontakt offen" if broken else "keine", ua.VariantType.String)


if __name__ == "__main__":
    asyncio.run(main())
