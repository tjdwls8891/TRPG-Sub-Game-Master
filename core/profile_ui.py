# 사전 저장 프로필 관리 — DM 인터페이스
#
# [기획 규정] 서버 홈에서 관리 UI 접촉 시 DM에 인터페이스를 연다.
#   생성 · 출력 · 수정 · 삭제 4메뉴.
#
# [출력]  시나리오별로 나누고 이름과 미리보기 목록. 동명이면 태그 표시.
#         이름 1차, 필요 시 태그 2차 검색. 하단에 이전·다음·재검색·돌아가기.
# [수정]  출력과 동일한 미리보기 → 검색으로 선택 → 항목 이름 입력으로 수정 개시.
#         긴 내용은 원문 복사 제공. 제한사항 안내. 조건 충족 확인.
# [삭제]  목록 셀렉터 → 개별 내용 표시 → 일일이 확인 메시지.
import discord

from . import profiles
from . import stats

PAGE_SIZE = 5


def _dup(user_id, scenario_id=None):
    return profiles.duplicate_names(user_id, scenario_id)


class ProfileHomeView(discord.ui.View):
    """DM 최상위 메뉴 — 생성·출력·수정·삭제."""

    def __init__(self, bot, user_id):
        super().__init__(timeout=900)
        self.bot = bot
        self.user_id = str(user_id)

    @discord.ui.button(label="✏️ 생성", style=discord.ButtonStyle.success)
    async def create(self, interaction, _b):
        # 기획 규정 — 사전 프로필 생성은 플레이해 본 시나리오만 가능하다.
        played = stats.load_stats(self.user_id).get("played_scenarios") or []
        if not played:
            await interaction.response.send_message(
                "아직 플레이한 시나리오가 없습니다.\n"
                "사전 프로필은 한 번이라도 플레이한 시나리오에 대해서만 만들 수 있습니다.\n"
                "공통 프로필(이름·성별·나이·외형)은 언제든 만들 수 있습니다.",
                ephemeral=False)
            return
        await interaction.response.send_message(
            "생성할 시나리오를 고르십시오.",
            view=ScenarioPickView(self.bot, self.user_id, played))

    @discord.ui.button(label="📋 출력", style=discord.ButtonStyle.primary)
    async def show(self, interaction, _b):
        items = profiles.list_profiles(self.user_id)
        if not items:
            await interaction.response.send_message("저장된 프로필이 없습니다.")
            return
        await interaction.response.send_message(
            embed=build_list_embed(self.user_id, items, 0),
            view=ProfileListView(self.bot, self.user_id, items, mode="view"))

    @discord.ui.button(label="🔧 수정", style=discord.ButtonStyle.secondary)
    async def edit(self, interaction, _b):
        items = profiles.list_profiles(self.user_id)
        if not items:
            await interaction.response.send_message("수정할 프로필이 없습니다.")
            return
        await interaction.response.send_message(
            embed=build_list_embed(self.user_id, items, 0),
            view=ProfileListView(self.bot, self.user_id, items, mode="edit"))

    @discord.ui.button(label="🗑 삭제", style=discord.ButtonStyle.danger)
    async def remove(self, interaction, _b):
        items = profiles.list_profiles(self.user_id)
        if not items:
            await interaction.response.send_message("삭제할 프로필이 없습니다.")
            return
        await interaction.response.send_message(
            "삭제할 프로필을 선택하십시오.",
            view=ProfileSelectView(self.bot, self.user_id, items, mode="delete"))


def build_list_embed(user_id, items: list, page: int) -> discord.Embed:
    """시나리오별로 나눈 목록. 동명이면 태그를 표시한다."""
    dup = _dup(user_id)
    total = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total - 1))
    chunk = items[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

    embed = discord.Embed(
        title="👤 사전 저장 프로필",
        description=f"{len(items)}개 · {page + 1}/{total} 페이지",
        color=0x9B59B6,
    )
    grouped = {}
    for p in chunk:
        grouped.setdefault(p.get("scenario_id") or "공통", []).append(p)
    for scenario, group in grouped.items():
        embed.add_field(
            name=scenario,
            value="\n".join(profiles.preview(p, dup) for p in group),
            inline=False,
        )
    return embed


class ProfileListView(discord.ui.View):
    """목록 탐색 — 이전·다음·재검색·돌아가기 (기획 규정)."""

    def __init__(self, bot, user_id, items, *, mode="view", page=0):
        super().__init__(timeout=600)
        self.bot = bot
        self.user_id = str(user_id)
        self.items = items
        self.mode = mode
        self.page = page

    def _total(self):
        return max(1, (len(self.items) + PAGE_SIZE - 1) // PAGE_SIZE)

    @discord.ui.button(label="◀ 이전", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction, _b):
        self.page = (self.page - 1) % self._total()
        await interaction.response.edit_message(
            embed=build_list_embed(self.user_id, self.items, self.page), view=self)

    @discord.ui.button(label="▶ 다음", style=discord.ButtonStyle.secondary)
    async def next(self, interaction, _b):
        self.page = (self.page + 1) % self._total()
        await interaction.response.edit_message(
            embed=build_list_embed(self.user_id, self.items, self.page), view=self)

    @discord.ui.button(label="🔍 검색", style=discord.ButtonStyle.primary)
    async def search(self, interaction, _b):
        await interaction.response.send_modal(SearchModal(self.bot, self.user_id, self.mode))

    @discord.ui.button(label="↩ 돌아가기", style=discord.ButtonStyle.secondary)
    async def back(self, interaction, _b):
        await interaction.response.edit_message(
            content="메뉴로 돌아갑니다.", embed=None,
            view=ProfileHomeView(self.bot, self.user_id))


class SearchModal(discord.ui.Modal, title="프로필 검색"):
    """이름 1차 검색. 동명이면 태그로 2차 검색한다(기획 규정)."""

    name = discord.ui.TextInput(label="이름", placeholder="예: 유이설", required=True)
    tag = discord.ui.TextInput(label="태그 (동명일 때만)", required=False,
                               placeholder="예: 설산")

    def __init__(self, bot, user_id, mode="view"):
        super().__init__()
        self.bot = bot
        self.user_id = str(user_id)
        self.mode = mode

    async def on_submit(self, interaction: discord.Interaction):
        found = profiles.search(self.user_id, str(self.name))
        if not found:
            await interaction.response.send_message(
                f"'{self.name}'을(를) 찾지 못했습니다.", ephemeral=False)
            return

        if len(found) > 1:
            tag = str(self.tag or "").strip()
            if not tag:
                tags = ", ".join(p.get("tag", "?") for p in found)
                await interaction.response.send_message(
                    f"동명 프로필이 {len(found)}개 있습니다. 태그로 다시 검색해 주십시오.\n"
                    f"> 태그: {tags}")
                return
            picked = profiles.search_by_tag(found, tag)
            if not picked:
                await interaction.response.send_message(
                    f"태그 '{tag}'를 찾지 못했습니다.")
                return
        else:
            picked = found[0]

        dup = _dup(self.user_id)
        if self.mode == "edit":
            await interaction.response.send_message(
                profiles.detail(picked, dup),
                view=EditFieldView(self.bot, self.user_id, picked))
        else:
            await interaction.response.send_message(profiles.detail(picked, dup))


class EditFieldView(discord.ui.View):
    """항목 이름을 입력받아 수정을 개시한다(기획 규정)."""

    def __init__(self, bot, user_id, profile):
        super().__init__(timeout=600)
        self.bot = bot
        self.user_id = str(user_id)
        self.profile = profile

    @discord.ui.button(label="✏️ 항목 수정", style=discord.ButtonStyle.primary)
    async def edit(self, interaction, _b):
        await interaction.response.send_modal(
            EditModal(self.bot, self.user_id, self.profile))

    @discord.ui.button(label="↩ 취소", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, _b):
        await interaction.response.edit_message(
            content="수정을 취소했습니다.", view=None)


class EditModal(discord.ui.Modal, title="항목 수정"):
    """
    수정 입력.

    NOTE: 긴 내용은 원문을 기본값으로 채워 복사·편집이 쉽게 한다(기획 규정).
          제한사항은 placeholder로 안내하고, 제출 시 조건을 확인한다.
    """

    field = discord.ui.TextInput(label="수정할 항목 이름",
                                 placeholder="예: 외형, 배경, 이름", required=True)
    value = discord.ui.TextInput(label="새 내용", style=discord.TextStyle.paragraph,
                                 required=True, max_length=1000)

    def __init__(self, bot, user_id, profile):
        super().__init__()
        self.bot = bot
        self.user_id = str(user_id)
        self.profile = profile

    async def on_submit(self, interaction: discord.Interaction):
        field = str(self.field).strip()
        value = str(self.value).strip()

        valid = set(profiles.SCENARIO_FIELDS) | set(
            (self.profile.get("fields") or {}).keys()) | {"이름"}
        if field not in valid:
            await interaction.response.send_message(
                f"'{field}'은(는) 수정 가능한 항목이 아닙니다.\n"
                f"> 가능: {', '.join(sorted(valid))}")
            return
        if not value:
            await interaction.response.send_message("내용이 비어 있습니다.")
            return

        key = "name" if field == "이름" else field
        ok = await profiles.update(self.user_id, self.profile["id"], key, value)
        await interaction.response.send_message(
            f"✅ '{field}'을(를) 수정했습니다." if ok else "⚠️ 수정에 실패했습니다.")


class ProfileSelectView(discord.ui.View):
    """삭제용 셀렉터 — 선택 후 개별 확인 메시지를 띄운다(기획 규정)."""

    def __init__(self, bot, user_id, items, *, mode="delete"):
        super().__init__(timeout=600)
        self.bot = bot
        self.user_id = str(user_id)
        self.mode = mode
        dup = _dup(user_id)
        options = [
            discord.SelectOption(
                label=profiles.display_name(p, dup)[:100],
                value=p["id"],
                description=(p.get("scenario_id") or "공통")[:100],
            )
            for p in items[:25]
        ]
        self.add_item(ProfileSelect(bot, self.user_id, options, mode))


class ProfileSelect(discord.ui.Select):
    def __init__(self, bot, user_id, options, mode):
        super().__init__(placeholder="프로필 선택", options=options)
        self.bot = bot
        self.user_id = user_id
        self.mode = mode

    async def callback(self, interaction: discord.Interaction):
        pid = self.values[0]
        picked = next((p for p in profiles.list_profiles(self.user_id)
                       if p.get("id") == pid), None)
        if not picked:
            await interaction.response.send_message("프로필을 찾을 수 없습니다.")
            return
        dup = _dup(self.user_id)
        await interaction.response.send_message(
            f"{profiles.detail(picked, dup)}\n\n"
            f"⚠️ **이 프로필을 삭제하시겠습니까?** 되돌릴 수 없습니다.",
            view=DeleteConfirmView(self.bot, self.user_id, picked))


class DeleteConfirmView(discord.ui.View):
    def __init__(self, bot, user_id, profile):
        super().__init__(timeout=300)
        self.bot = bot
        self.user_id = str(user_id)
        self.profile = profile

    @discord.ui.button(label="삭제", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, _b):
        ok = await profiles.delete(self.user_id, self.profile["id"])
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(
            content=f"{'🗑 삭제했습니다.' if ok else '⚠️ 삭제에 실패했습니다.'}",
            view=self)

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, _b):
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(content="삭제를 취소했습니다.", view=self)


class ScenarioPickView(discord.ui.View):
    """생성 시 시나리오 선택. 공통 프로필도 고를 수 있다."""

    def __init__(self, bot, user_id, played: list):
        super().__init__(timeout=600)
        self.bot = bot
        self.user_id = str(user_id)
        options = [discord.SelectOption(label="공통 프로필", value="__common__",
                                        description="이름·성별·나이·외형")]
        options += [discord.SelectOption(label=s[:100], value=s) for s in played[:24]]
        self.add_item(ScenarioSelect(bot, self.user_id, options))


class ScenarioSelect(discord.ui.Select):
    def __init__(self, bot, user_id, options):
        super().__init__(placeholder="시나리오 선택", options=options)
        self.bot = bot
        self.user_id = user_id

    async def callback(self, interaction: discord.Interaction):
        sid = None if self.values[0] == "__common__" else self.values[0]
        fields = (profiles.COMMON_FIELDS if sid is None
                  else profiles.SCENARIO_FIELDS)
        await interaction.response.send_message(
            f"**{sid or '공통 프로필'}** 생성\n"
            f"> 채울 항목: {', '.join(fields)}\n"
            f"> 저장 전까지 항목을 수정할 수 있습니다.\n\n"
            f"생성 인터페이스는 세션 생성 플로우와 함께 활성화됩니다."
        )


async def open_manager(bot, user) -> bool:
    """DM으로 관리 인터페이스를 연다."""
    try:
        await user.send(
            "👤 **사전 저장 프로필 관리**\n"
            "생성·출력·수정·삭제를 선택하십시오.",
            view=ProfileHomeView(bot, user.id),
        )
        return True
    except discord.Forbidden:
        return False
    except Exception as e:
        print(f"[프로필관리] DM 실패: {e}")
        return False
