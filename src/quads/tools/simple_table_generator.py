#!/usr/bin/env python3
import argparse
import asyncio
import os
import random
from datetime import datetime
from typing import List
from urllib import parse as url_parse

import aiohttp
from aiohttp import BasicAuth
from jinja2 import Template
from requests import Response

from quads.config import Config
from quads.helpers.utils import first_day_month
from quads.server.models import Schedule, Host


class QuadsApiAsync:

    def __init__(self, config):
        self.config = config
        self.base_url = config.API_URL

    async def async_get(self, endpoint: str) -> Response:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    os.path.join(self.base_url, endpoint),
                    auth=BasicAuth(self.config.get("quads_api_username"), self.config.get("quads_api_password")),
                    timeout=60,
                    verify_ssl=False,
                ) as response:
                    result = await response.json()
        except Exception:
            result = {}
        return result

    async def get_monthly_schedules(self, start: datetime, end: datetime) -> List[Schedule]:
        endpoint = os.path.join("schedules")
        url = f"{endpoint}"
        data = {
            "start__lte": end.strftime("%Y-%m-%dT22:00"),
            "end__gte": start.strftime("%Y-%m-%dT22:00"),
        }
        if data:
            url_params = url_parse.urlencode(data)
            url = f"{endpoint}?{url_params}"
        response = await self.async_get(url)
        schedules = []
        for schedule in response:
            schedules.append(Schedule().from_dict(schedule))
        return schedules

    async def async_filter_hosts(self, data) -> List[Host]:
        url_params = url_parse.urlencode(data)
        response = await self.async_get(f"hosts?{url_params}")
        hosts = []
        for host in response:
            host_obj = Host().from_dict(data=host)
            hosts.append(host_obj)
        return hosts


class HostGenerate:

    BLOCK_SIZE = 10

    def __init__(self):
        self.hosts = []
        self.total_current_schedules = {}
        self.quads_async = QuadsApiAsync(Config)
        self.colors = []
        self.emojis = []
        self.current_host_schedules = {}
        self.generate_colors()

    def random_color(self):
        def rand():
            return random.randint(100, 255)

        return "#%02X%02X%02X" % (rand(), rand(), rand())

    def generate_colors(self):
        all_samples = []
        all_samples.extend(range(129296, 129510))
        all_samples.extend(range(128000, 128252))
        samples = random.sample(all_samples, 200)
        exclude = [129401, 129484]
        self.emojis = [emoji for emoji in samples if emoji not in exclude]
        self.colors = [self.random_color() for _ in range(100)]
        self.colors[0] = "#A9A9A9"

    async def order_current_schedules_by_hostname(self, first_day, last_day):
        if not self.total_current_schedules:
            total_current_schedules = await self.quads_async.get_monthly_schedules(first_day, last_day)
            for schedule in total_current_schedules:
                host_schedules = self.total_current_schedules.get(schedule.host.name)
                if host_schedules:
                    host_schedules.append(schedule)
                else:
                    self.total_current_schedules[schedule.host.name] = [schedule]

    async def get_current_host_schedules(self, host_name, first_day, last_day):
        if not self.total_current_schedules:
            await self.order_current_schedules_by_hostname(first_day, last_day)
        return self.total_current_schedules.get(host_name, None)

    async def process_hosts(self, host, _days, _month, _year, first_day, last_day):
        non_allocated_count = 0
        __days = []
        schedules = await self.get_current_host_schedules(host.name, first_day, last_day)
        for j in range(1, _days + 1):
            chosen_color = "01"
            cell_date = "%s-%.2d-%.2d 01:00" % (_year, _month, j)
            cell_time = datetime.strptime(cell_date, "%Y-%m-%d %H:%M")
            _day = {
                "day": j,
                "chosen_color": chosen_color,
                "emoji": "&#%s;" % self.emojis[int(chosen_color) - 1],
                "color": self.colors[int(chosen_color) - 1],
                "cell_date": cell_date,
                "cell_time": cell_time,
            }
            if schedules:
                for schedule in schedules:
                    chosen_color = schedule.assignment.cloud.name[5:] if schedules else "01"
                    schedule_start_date = schedule.start
                    schedule_end_date = schedule.end
                    if schedule_start_date <= cell_time <= schedule_end_date:
                        assignment = schedule.assignment
                        _day["display_description"] = assignment.description
                        _day["display_owner"] = assignment.owner
                        _day["display_ticket"] = assignment.ticket
                        _day["chosen_color"] = chosen_color
                        _day["emoji"] = "&#%s;" % self.emojis[int(chosen_color) - 1]
                        _day["color"] = self.colors[int(chosen_color) - 1]
                        break
            else:
                non_allocated_count += 1
            __days.append(_day)
        return __days, non_allocated_count

    async def generator(self, _days, _month, _year, _gentime):
        self.hosts = await self.quads_async.async_filter_hosts(data={"retired": False, "broken": False})
        last_day = datetime(_year, _month, _days)
        first_day = first_day_month(last_day)
        if not self.total_current_schedules:
            await self.order_current_schedules_by_hostname(first_day, last_day)

        total_current_schedules = self.total_current_schedules
        lines = []
        non_allocated_count = 0
        host_blocks = [self.hosts[i : i + self.BLOCK_SIZE] for i in range(0, len(self.hosts), self.BLOCK_SIZE)]

        for host_block in host_blocks:
            tasks = [self.process_hosts(host, _days, _month, _year, first_day, last_day) for host in host_block]
            results = await asyncio.gather(*tasks)

            for host, (days, non_allocated) in zip(host_block, results):
                lines.append({"hostname": host.name, "days": days})
                non_allocated_count += non_allocated

        total_hosts = len(self.hosts)
        total_use = len(total_current_schedules)
        if int(total_hosts) == 0:
            utilization_daily = 0
            utilization = 0
        else:
            utilization = 100 - (non_allocated_count * 100 // (_days * total_hosts))
            utilization_daily = total_use * 100 // total_hosts
        with open(os.path.join(Config.TEMPLATES_PATH, "simple_table_emoji")) as _file:
            template = Template(_file.read())
        content = template.render(
            gentime=_gentime,
            _days=_days,
            lines=lines,
            utilization=utilization,
            utilization_daily=utilization_daily,
            total_use=total_use,
            total_hosts=total_hosts,
        )

        return content


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate a simple HTML table with color depicting resource usage for the month"
    )
    requiredArgs = parser.add_argument_group("Required Arguments")
    requiredArgs.add_argument(
        "-d",
        "--days",
        dest="days",
        type=int,
        required=True,
        default=None,
        help="number of days to generate",
    )
    requiredArgs.add_argument(
        "-m",
        "--month",
        dest="month",
        type=str,
        required=True,
        default=None,
        help="Month to generate",
    )
    requiredArgs.add_argument(
        "-y",
        "--year",
        dest="year",
        type=str,
        required=True,
        default=None,
        help="Year to generate",
    )
    parser.add_argument(
        "--gentime",
        "-g",
        dest="gentime",
        type=str,
        required=False,
        default=None,
        help="generate timestamp when created",
    )

    args = parser.parse_args()
    days = args.days
    month = args.month
    year = args.year
    gentime = args.gentime

    generate = HostGenerate()
    asyncio.run(generate.generator(days, month, year, gentime))
