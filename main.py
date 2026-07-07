from __future__ import annotations

import asyncio
from typing import Any, Optional
from urllib.parse import quote

import aiohttp
from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import AstrBotConfig


# mihomo/clash-meta 中，支持手动切换节点的代理组类型
SELECTABLE_GROUP_TYPES = {"Selector", "URLTest", "LoadBalance", "Fallback"}


class MihomoClient:
    """对 mihomo external-controller RESTful API 的轻量封装。

    主要端点参考：
        GET  /proxies                 获取所有代理及组
        GET  /proxies/{name}           获取某个代理/组的详细信息
        GET  /proxies/{name}/delay    测试某个代理的延迟
        PUT  /proxies/{name}          切换 Selector/Fallback 等组中的当前代理
    """

    def __init__(
        self,
        host: str,
        port: int,
        secret: str = "",
        delay_timeout: int = 5000,
        delay_url: str = "http://www.gstatic.com/generate_204",
    ):
        self.base_url = f"http://{host}:{port}".rstrip("/")
        self.secret = (secret or "").strip()
        self.delay_timeout = delay_timeout
        self.delay_url = delay_url
        self._session: Optional[aiohttp.ClientSession] = None

    def _build_headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.secret:
            # mihomo 兼容旧版 clash 认证方式，使用 Bearer token
            headers["Authorization"] = f"Bearer {self.secret}"
        return headers

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=30)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
    ) -> Any:
        session = await self._get_session()
        url = f"{self.base_url}{path}"
        try:
            async with session.request(
                method,
                url,
                params=params,
                json=json_body,
                headers=self._build_headers(),
            ) as resp:
                # 504/408 一般代表节点测速超时；仍尝试解析返回体
                text = await resp.text()
                if not text:
                    return None
                try:
                    return await resp.json() if resp.content_type == "application/json" else text
                except Exception:
                    return text
        except aiohttp.ClientError as e:
            logger.error(f"[mihomo] 请求失败: {method} {url} -> {e}")
            raise

    # ---------- 业务接口 ----------

    async def get_proxies(self) -> dict[str, Any]:
        """获取所有代理及其详细信息。"""
        return await self._request("GET", "/proxies")

    async def get_proxy(self, name: str) -> dict[str, Any]:
        """获取某个代理/组的详细信息。"""
        return await self._request("GET", f"/proxies/{quote(name, safe='')}")

    async def get_delay(self, name: str) -> Optional[int]:
        """测试某个代理的延迟（毫秒）。

        返回:
            int: 延迟毫秒数。
            None: 测试失败（节点不可达、认证失败等）。
        """
        try:
            data = await self._request(
                "GET",
                f"/proxies/{quote(name, safe='')}/delay",
                params={"timeout": self.delay_timeout, "url": self.delay_url},
            )
        except Exception:
            return None
        if isinstance(data, dict):
            delay = data.get("delay")
            if isinstance(delay, (int, float)):
                return int(delay)
        return None

    async def select_proxy(self, group: str, proxy: str) -> bool:
        """切换指定代理组下选中的代理（仅对 Selector/Fallback 等类型有效）。"""
        try:
            await self._request(
                "PUT",
                f"/proxies/{quote(group, safe='')}",
                json_body={"name": proxy},
            )
            return True
        except Exception as e:
            logger.error(f"[mihomo] 切换代理失败: group={group} proxy={proxy} -> {e}")
            return False


@register(
    "astrbot_plugin_mihomo_dashborad",
    "zhoufan",
    "通过 mihomo 的 RESTful API 管理代理组与节点",
    "1.0.0",
)
class MihomoDashboardPlugin(Star):
    # 选择会话上下文的过期时间（秒），超过该时间 /选择 指令将失效
    _SELECTION_TTL = 5 * 60

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.client = self._build_client()
        # 保存每个 session 最近一次 /代理测速 的结果：
        #   key   = event.session_id
        #   value = {"group": str, "nodes": {编号(int): 节点名(str)}, "expire_at": float}
        self._pending_selections: dict[str, dict[str, Any]] = {}

    def _cleanup_expired_selections(self):
        """清理已过期的测速上下文，避免字典无限增长。"""
        import time

        now = time.time()
        expired = [
            k for k, v in self._pending_selections.items()
            if v.get("expire_at", 0) < now
        ]
        for k in expired:
            self._pending_selections.pop(k, None)

    def _build_client(self) -> MihomoClient:
        host = str(self.config.get("mihomo_host", "127.0.0.1") or "127.0.0.1")
        port = int(self.config.get("mihomo_port", 9090) or 9090)
        secret = str(self.config.get("mihomo_secret", "") or "")
        delay_timeout = int(self.config.get("delay_timeout", 5000) or 5000)
        delay_url = str(
            self.config.get(
                "delay_url", "http://www.gstatic.com/generate_204"
            )
            or "http://www.gstatic.com/generate_204"
        )
        return MihomoClient(
            host=host,
            port=port,
            secret=secret,
            delay_timeout=delay_timeout,
            delay_url=delay_url,
        )

    async def initialize(self):
        """插件加载时的初始化钩子。AstrBot 会自动调用。"""
        logger.info(
            f"[mihomo] 插件已加载，连接到 http://{self.client.base_url}"
        )

    async def terminate(self):
        """插件卸载/停用时的清理钩子。"""
        await self.client.close()

    # ---------- 工具方法 ----------

    async def _list_groups(self) -> list[dict[str, Any]]:
        """获取所有代理组（type 为 Selector/URLTest/LoadBalance/Fallback）。"""
        data = await self.client.get_proxies()
        proxies = (data or {}).get("proxies", {}) if isinstance(data, dict) else {}
        groups: list[dict[str, Any]] = []
        for name, info in proxies.items():
            if not isinstance(info, dict):
                continue
            if info.get("type") in SELECTABLE_GROUP_TYPES:
                groups.append({"name": name, **info})
        # 排序，保证输出稳定
        groups.sort(key=lambda x: x.get("name", ""))
        return groups

    @staticmethod
    def _format_delay(delay: Optional[int]) -> str:
        if delay is None:
            return "超时"
        if delay <= 0:
            return "0 ms"
        return f"{delay} ms"

    @staticmethod
    def _build_group_lines(groups: list[dict[str, Any]]) -> str:
        if not groups:
            return "（暂无代理组）"
        lines: list[str] = []
        for idx, g in enumerate(groups, 1):
            name = g.get("name", "?")
            gtype = g.get("type", "?")
            now = g.get("now", "-") or "-"
            all_count = len(g.get("all", []) or [])
            lines.append(f"{idx}. {name}  [类型: {gtype}, 节点数: {all_count}, 当前: {now}]")
        return "\n".join(lines)

    # ---------- 指令 ----------

    @filter.command("代理查询")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def list_groups(self, event: AstrMessageEvent):
        """列出当前 mihomo 配置文件中的所有代理组。"""
        try:
            groups = await self._list_groups()
        except Exception as e:
            yield event.plain_result(f"查询代理组失败：无法连接到 mihomo API。\n错误：{e}")
            return

        yield event.plain_result(
            "📋 当前 mihomo 代理组列表：\n" + self._build_group_lines(groups)
        )

    @filter.command("代理测速")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def test_group(self, event: AstrMessageEvent, group: str):
        """测试指定代理组下所有节点的延迟。返回的节点前会加上编号，
        用户可通过 /选择 <编号> 或 /代理选择 <编号> 进行快速切换。"""
        try:
            info = await self.client.get_proxy(group)
        except Exception as e:
            yield event.plain_result(f"获取代理组信息失败：{e}")
            return

        if not isinstance(info, dict):
            yield event.plain_result(f"未找到代理组：{group}")
            return

        all_nodes: list[str] = info.get("all", []) or []
        if not all_nodes:
            yield event.plain_result(f"代理组「{group}」下没有任何节点。")
            return

        yield event.plain_result(
            f"⏱ 正在测试代理组「{group}」下的 {len(all_nodes)} 个节点，请稍候..."
        )

        # 并发测速
        results: list[tuple[str, Optional[int]]] = await asyncio.gather(
            *(self._delay_one(n) for n in all_nodes)
        )

        # 按延迟升序排序，超时放末尾
        results.sort(key=lambda x: (x[1] is None, x[1] if x[1] is not None else 0))

        # 给节点重新编号（从 1 开始，按延迟从低到高）
        indexed: list[tuple[int, str, Optional[int]]] = [
            (idx + 1, name, delay) for idx, (name, delay) in enumerate(results)
        ]

        # 保存会话上下文，供 /选择 指令使用（按 session_id 隔离，多人互不干扰）
        import time

        self._cleanup_expired_selections()
        self._pending_selections[event.session_id] = {
            "group": group,
            "nodes": {idx: name for idx, name, _ in indexed},
            "expire_at": time.time() + self._SELECTION_TTL,
        }

        lines = [
            f"🔍 代理组「{group}」测速结果（从快到慢）：",
            "💡 回复 /选择 <编号> 可快速切换，例如：/选择 1",
        ]
        for idx, name, delay in indexed:
            lines.append(f"  {idx:>2}. {name}: {self._format_delay(delay)}")
        yield event.plain_result("\n".join(lines))

    async def _delay_one(self, name: str) -> tuple[str, Optional[int]]:
        try:
            return name, await self.client.get_delay(name)
        except Exception:
            return name, None

    @filter.command("代理切换")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def switch_proxy(self, event: AstrMessageEvent, group: str, proxy: str):
        """切换指定代理组下的选中节点。"""
        try:
            info = await self.client.get_proxy(group)
        except Exception as e:
            yield event.plain_result(f"获取代理组信息失败：{e}")
            return

        if not isinstance(info, dict):
            yield event.plain_result(f"未找到代理组：{group}")
            return

        if info.get("type") not in SELECTABLE_GROUP_TYPES:
            yield event.plain_result(
                f"代理组「{group}」类型为「{info.get('type')}」，不支持手动切换。"
            )
            return

        all_nodes: list[str] = info.get("all", []) or []
        if proxy not in all_nodes:
            # 给出一些相似建议
            hint = ""
            for n in all_nodes:
                if proxy in n or n in proxy:
                    hint = f"\n您是否想切换到：{n}？"
                    break
            yield event.plain_result(
                f"代理组「{group}」下不存在节点「{proxy}」。{hint}"
            )
            return

        ok = await self.client.select_proxy(group, proxy)
        if ok:
            yield event.plain_result(
                f"✅ 已将代理组「{group}」切换为「{proxy}」。"
            )
        else:
            yield event.plain_result(
                f"❌ 切换失败，请确认 mihomo 配置是否允许手动切换该组。"
            )

    @filter.command("选择")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def select_by_index(self, event: AstrMessageEvent, index: int):
        """通过 /代理测速 输出中的编号切换代理组节点。

        该指令依赖当前会话最近一次 /代理测速 的结果，过期时间为 5 分钟。
        """
        import time

        self._cleanup_expired_selections()
        pending = self._pending_selections.get(event.session_id)
        if not pending:
            yield event.plain_result(
                "⚠️ 当前会话没有可用的测速结果，请先使用 /代理测速 <组名>。"
            )
            return

        group: str = pending["group"]
        nodes: dict[int, str] = pending["nodes"]

        if index not in nodes:
            yield event.plain_result(
                f"❌ 编号 {index} 不在范围内（1 - {len(nodes)}）。请使用 /代理测速 <组名> 重新测速。"
            )
            return

        proxy = nodes[index]
        ok = await self.client.select_proxy(group, proxy)
        if ok:
            yield event.plain_result(
                f"✅ 已将代理组「{group}」切换为「{proxy}」。"
            )
            # 切换成功后清理会话状态，避免误用
            self._pending_selections.pop(event.session_id, None)
        else:
            yield event.plain_result(
                f"❌ 切换失败，请确认 mihomo 配置是否允许手动切换该组。"
            )

    @filter.command("代理选择")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def select_by_index_alias(self, event: AstrMessageEvent, index: int):
        """/选择 指令的别名。"""
        async for r in self.select_by_index(event, index):
            yield r

    @filter.command("代理状态")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def status(self, event: AstrMessageEvent):
        """查询所有代理组当前选中节点的延迟。"""
        try:
            groups = await self._list_groups()
        except Exception as e:
            yield event.plain_result(f"查询代理组失败：{e}")
            return

        if not groups:
            yield event.plain_result("（暂无代理组）")
            return

        # 并发测试每个组当前节点的延迟
        async def _delay_or_none(node: Optional[str]) -> Optional[int]:
            if not node:
                return None
            try:
                return await self.client.get_delay(node)
            except Exception:
                return None

        delay_map: dict[str, Optional[int]] = {}
        now_list = [g.get("now") for g in groups]
        delays = await asyncio.gather(*(_delay_or_none(n) for n in now_list))
        for node, d in zip(now_list, delays):
            if node:
                delay_map[node] = d

        lines = ["📊 当前代理组状态："]
        for g in groups:
            now = g.get("now") or "-"
            delay = delay_map.get(now) if now != "-" else None
            lines.append(
                f"  - {g.get('name')} [{g.get('type')}] 当前: {now}  延迟: {self._format_delay(delay)}"
            )
        yield event.plain_result("\n".join(lines))

    @filter.command("代理最优")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def best(self, event: AstrMessageEvent):
        """查询所有代理组下延迟最低的节点，并切换。"""
        try:
            groups = await self._list_groups()
        except Exception as e:
            yield event.plain_result(f"查询代理组失败：{e}")
            return

        if not groups:
            yield event.plain_result("（暂无代理组）")
            return

        yield event.plain_result(
            f"🔎 正在为 {len(groups)} 个代理组寻找最优节点，请稍候..."
        )

        lines = ["🏆 最优节点结果："]
        # 对每个组并发测速
        tasks = [self._find_best_for_group(g) for g in groups]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        any_changed = False
        for g, res in zip(groups, results):
            gname = g.get("name", "?")
            if isinstance(res, Exception) or res is None:
                lines.append(f"  - {gname}: 测速失败，跳过")
                continue
            best_name, best_delay, switched, msg = res
            tag = "已切换" if switched else "保持不变"
            lines.append(
                f"  - {gname}: 最优={best_name} ({self._format_delay(best_delay)}) {tag}"
            )
            if msg:
                lines.append(f"      {msg}")
            if switched:
                any_changed = True

        if not any_changed:
            lines.append("\n（提示：部分组类型如 url-test/load-balance/fallback 是自动选线的，无需手动切换）")
        yield event.plain_result("\n".join(lines))

    async def _find_best_for_group(
        self, g: dict[str, Any]
    ) -> tuple[str, Optional[int], bool, str]:
        """为单个代理组找到延迟最低的节点，并尝试切换。

        返回: (最优节点名, 延迟, 是否已切换, 额外提示)
        """
        name = g.get("name", "?")
        all_nodes: list[str] = g.get("all", []) or []
        if not all_nodes:
            return name, None, False, "该组下无节点"

        results = await asyncio.gather(*(self._delay_one(n) for n in all_nodes))
        # 过滤掉超时节点
        valid = [(n, d) for n, d in results if d is not None]
        if not valid:
            return name, None, False, "所有节点均超时"

        valid.sort(key=lambda x: x[1])
        best_name, best_delay = valid[0]

        # 对自动选线组（url-test/load-balance/fallback），无法手动切换
        gtype = g.get("type")
        if gtype in {"URLTest", "LoadBalance", "Fallback"}:
            return best_name, best_delay, False, f"组类型 {gtype} 由内核自动选线"

        # 已是当前节点则不重复切换
        if g.get("now") == best_name:
            return best_name, best_delay, False, "当前已是延迟最低节点"

        ok = await self.client.select_proxy(name, best_name)
        return best_name, best_delay, ok, ""