"""
بوت ديسكورد - نظام الرومات الصوتية المؤقتة
--------------------------------------------
- أمر /setup لإنشاء الكاتيجوري وروم الإنشاء (للإدمن فقط)
- عند دخول أي عضو لروم الإنشاء، يتم إنشاء روم خاص به تلقائياً
- كل روم يحصل على لوحة تحكم كاملة (أزرار) خاصة بصاحب الروم فقط
- حفظ كل البيانات في ملفات JSON (config.json و rooms.json)
"""

import os
import asyncio

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

import storage

# ---------------------------------------------------------------------------
# الإعدادات الأساسية
# ---------------------------------------------------------------------------

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

CONFIG_FILE = "config.json"
ROOMS_FILE = "rooms.json"

intents = discord.Intents.default()
intents.voice_states = True
intents.members = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)

config_data: dict = storage.load_json(CONFIG_FILE)
rooms_data: dict = storage.load_json(ROOMS_FILE)


def save_config() -> None:
    storage.save_json(CONFIG_FILE, config_data)


def save_rooms() -> None:
    storage.save_json(ROOMS_FILE, rooms_data)


def get_guild_config(guild_id: int) -> dict | None:
    return config_data.get(str(guild_id))


def get_room(channel_id: int) -> dict | None:
    return rooms_data.get(str(channel_id))


def find_owned_room(guild_id: int, user_id: int) -> str | None:
    """يبحث إذا كان العضو يملك رومًا بالفعل في هذا السيرفر، ويرجع آيدي الروم أو None."""
    for channel_id, data in rooms_data.items():
        if data.get("guild_id") == guild_id and data.get("owner_id") == user_id:
            return channel_id
    return None


def is_owner(interaction: discord.Interaction) -> bool:
    room = get_room(interaction.channel.id)
    if room is None:
        return False
    return interaction.user.id == room["owner_id"]


# قفل غير متزامن يمنع إنشاء أكثر من روم لنفس العضو إذا وصل الحدث مرتين بسرعة
creation_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# بناء وتحديث لوحة التحكم (إيمبد حي يعكس حالة الروم الحالية)
# ---------------------------------------------------------------------------

def build_panel_embed(channel: discord.VoiceChannel, owner: discord.Member | None, locked: bool) -> discord.Embed:
    status_text = "🔒 مقفول" if locked else "🔓 مفتوح"
    limit_text = str(channel.user_limit) if channel.user_limit else "بدون حد"

    embed = discord.Embed(
        title=f"🎧 {channel.name}",
        description="لوحة تحكم الروم — الأزرار والقائمة أدناه متاحة لصاحب الروم فقط.",
        color=discord.Color.red() if locked else discord.Color.green(),
    )
    if owner:
        embed.set_thumbnail(url=owner.display_avatar.url)
        embed.add_field(name="👤 المالك", value=owner.mention, inline=True)
    embed.add_field(name="📶 الحالة", value=status_text, inline=True)
    embed.add_field(name="👥 الحد الأقصى", value=limit_text, inline=True)
    embed.add_field(name="🧑‍🤝‍🧑 المتصلون الآن", value=str(len(channel.members)), inline=True)
    embed.set_footer(text="Nova Rooms")
    return embed


async def refresh_panel(channel: discord.VoiceChannel) -> None:
    """يحدّث رسالة اللوحة (عبر جلبها بآيدي مخزّن) لتعكس آخر حالة. يُستخدم كخطة احتياطية فقط."""
    room = get_room(channel.id)
    if room is None or "panel_message_id" not in room:
        return
    try:
        msg = await channel.fetch_message(room["panel_message_id"])
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return

    owner = channel.guild.get_member(room["owner_id"])
    embed = build_panel_embed(channel, owner, room.get("locked", False))
    try:
        await msg.edit(embed=embed)
    except discord.HTTPException:
        pass


async def try_refresh(channel: discord.VoiceChannel, panel_message: discord.Message | None = None) -> None:
    """
    يحدّث اللوحة بأقل عدد ممكن من طلبات API:
    لو عندنا مرجع الرسالة جاهز (من الإنتراكشن نفسها) نعدّلها مباشرة بدون أي جلب إضافي،
    وإلا نرجع لطريقة الجلب كخطة احتياطية فقط.
    """
    room = get_room(channel.id)
    if room is None:
        return
    owner = channel.guild.get_member(room["owner_id"])
    embed = build_panel_embed(channel, owner, room.get("locked", False))

    if panel_message is not None:
        try:
            await panel_message.edit(embed=embed)
            return
        except discord.HTTPException:
            pass

    await refresh_panel(channel)


# ---------------------------------------------------------------------------
# النوافذ المنبثقة (Modals)
# ---------------------------------------------------------------------------

class RenameModal(discord.ui.Modal, title="تغيير اسم الروم"):
    new_name = discord.ui.TextInput(label="الاسم الجديد", max_length=90)

    def __init__(self, channel: discord.VoiceChannel, panel_message: discord.Message | None = None):
        super().__init__()
        self.channel = channel
        self.panel_message = panel_message

    async def on_submit(self, interaction: discord.Interaction):
        await self.channel.edit(name=str(self.new_name))
        await try_refresh(self.channel, self.panel_message)
        await interaction.response.send_message(
            f"✅ تم تغيير اسم الروم إلى **{self.new_name}**", ephemeral=True
        )


class LimitModal(discord.ui.Modal, title="تحديد عدد الأعضاء"):
    limit = discord.ui.TextInput(label="العدد (0 = بدون حد)", max_length=3)

    def __init__(self, channel: discord.VoiceChannel, panel_message: discord.Message | None = None):
        super().__init__()
        self.channel = channel
        self.panel_message = panel_message

    async def on_submit(self, interaction: discord.Interaction):
        try:
            value = int(str(self.limit))
        except ValueError:
            await interaction.response.send_message("❌ الرجاء إدخال رقم صحيح", ephemeral=True)
            return
        if value < 0 or value > 99:
            await interaction.response.send_message("❌ الرقم يجب أن يكون بين 0 و 99", ephemeral=True)
            return
        await self.channel.edit(user_limit=value)
        await try_refresh(self.channel, self.panel_message)
        await interaction.response.send_message(
            f"✅ تم تحديد عدد الأعضاء: **{value if value else 'بدون حد'}**", ephemeral=True
        )


class UserActionModal(discord.ui.Modal):
    """موديل عام يستخدم للطرد / الحظر / رفع الحظر / نقل الملكية."""

    user_input = discord.ui.TextInput(label="آيدي العضو أو منشن أو اسمه")

    def __init__(
        self,
        title: str,
        channel: discord.VoiceChannel,
        action: str,
        panel_message: discord.Message | None = None,
    ):
        super().__init__(title=title)
        self.channel = channel
        self.action = action
        self.panel_message = panel_message

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        raw = str(self.user_input).strip()
        cleaned = raw.replace("<@", "").replace(">", "").replace("!", "")

        member = None
        if cleaned.isdigit():
            member = guild.get_member(int(cleaned))
        if member is None:
            member = discord.utils.find(
                lambda m: raw.lower() in (m.name.lower(), (m.nick or "").lower(), m.display_name.lower()),
                guild.members,
            )

        if member is None:
            await interaction.response.send_message("❌ لم يتم العثور على هذا العضو", ephemeral=True)
            return

        room = get_room(self.channel.id)
        if room is None:
            await interaction.response.send_message("❌ هذا الروم غير مسجل في النظام", ephemeral=True)
            return

        if self.action == "kick":
            if member.voice and member.voice.channel and member.voice.channel.id == self.channel.id:
                await member.move_to(None, reason="طرد من صاحب الروم")
                await try_refresh(self.channel, self.panel_message)
                await interaction.response.send_message(f"👢 تم طرد {member.mention} من الروم", ephemeral=True)
            else:
                await interaction.response.send_message("❌ هذا العضو ليس داخل الروم حالياً", ephemeral=True)

        elif self.action == "transfer":
            if member.bot:
                await interaction.response.send_message("❌ لا يمكن نقل الملكية لبوت", ephemeral=True)
                return
            old_owner_id = room["owner_id"]
            room["owner_id"] = member.id
            save_rooms()

            try:
                old_owner = interaction.guild.get_member(old_owner_id)
                if old_owner:
                    await self.channel.set_permissions(old_owner, overwrite=None)
                new_overwrite = discord.PermissionOverwrite(
                    view_channel=True,
                    connect=True,
                    manage_channels=True,
                    move_members=True,
                    mute_members=True,
                    deafen_members=True,
                    priority_speaker=True,
                )
                await self.channel.set_permissions(member, overwrite=new_overwrite)
            except discord.Forbidden:
                pass

            await try_refresh(self.channel, self.panel_message)
            await interaction.response.send_message(f"👑 تم نقل ملكية الروم إلى {member.mention}", ephemeral=True)

        elif self.action == "block":
            overwrite = self.channel.overwrites_for(member)
            overwrite.connect = False
            await self.channel.set_permissions(member, overwrite=overwrite)
            if member.voice and member.voice.channel and member.voice.channel.id == self.channel.id:
                await member.move_to(None, reason="تم حظره من الروم")
            await try_refresh(self.channel, self.panel_message)
            await interaction.response.send_message(f"🚫 تم حظر {member.mention} من دخول الروم", ephemeral=True)

        elif self.action == "unblock":
            await self.channel.set_permissions(member, overwrite=None)
            await interaction.response.send_message(f"✅ تم رفع الحظر عن {member.mention}", ephemeral=True)


# ---------------------------------------------------------------------------
# لوحة التحكم (أزرار مباشرة، بدون قائمة منسدلة)
# ---------------------------------------------------------------------------

class ControlPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def owner_check(self, interaction: discord.Interaction) -> bool:
        if not is_owner(interaction):
            await interaction.response.send_message("❌ هذا الزر متاح فقط لصاحب الروم", ephemeral=True)
            return False
        return True

    # الصف الأول
    @discord.ui.button(label="قفل / فتح", emoji="🔒", style=discord.ButtonStyle.primary, custom_id="tv_toggle_lock", row=0)
    async def toggle_lock_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.owner_check(interaction):
            return
        channel = interaction.channel
        room = get_room(channel.id)

        new_locked = not room.get("locked", False)
        overwrite = channel.overwrites_for(interaction.guild.default_role)
        overwrite.connect = not new_locked
        await channel.set_permissions(interaction.guild.default_role, overwrite=overwrite)

        room["locked"] = new_locked
        save_rooms()

        owner = interaction.guild.get_member(room["owner_id"])
        embed = build_panel_embed(channel, owner, new_locked)
        # تعديل رسالة اللوحة نفسها مباشرة = طلب واحد فقط لـ API، بدون رسالة تأكيد إضافية
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="تغيير الاسم", emoji="✏️", style=discord.ButtonStyle.secondary, custom_id="tv_rename", row=0)
    async def rename_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.owner_check(interaction):
            return
        await interaction.response.send_modal(RenameModal(interaction.channel, interaction.message))

    @discord.ui.button(label="تحديد العدد", emoji="👥", style=discord.ButtonStyle.secondary, custom_id="tv_limit", row=0)
    async def limit_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.owner_check(interaction):
            return
        await interaction.response.send_modal(LimitModal(interaction.channel, interaction.message))

    # الصف الثاني
    @discord.ui.button(label="طرد", emoji="👢", style=discord.ButtonStyle.secondary, custom_id="tv_kick", row=1)
    async def kick_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.owner_check(interaction):
            return
        await interaction.response.send_modal(UserActionModal("طرد عضو", interaction.channel, "kick", interaction.message))

    @discord.ui.button(label="حظر", emoji="🚫", style=discord.ButtonStyle.secondary, custom_id="tv_block", row=1)
    async def block_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.owner_check(interaction):
            return
        await interaction.response.send_modal(UserActionModal("حظر عضو", interaction.channel, "block", interaction.message))

    @discord.ui.button(label="رفع حظر", emoji="✅", style=discord.ButtonStyle.secondary, custom_id="tv_unblock", row=1)
    async def unblock_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.owner_check(interaction):
            return
        await interaction.response.send_modal(UserActionModal("رفع الحظر عن عضو", interaction.channel, "unblock", interaction.message))

    # الصف الثالث
    @discord.ui.button(label="نقل الملكية", emoji="👑", style=discord.ButtonStyle.secondary, custom_id="tv_transfer", row=2)
    async def transfer_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.owner_check(interaction):
            return
        await interaction.response.send_modal(UserActionModal("نقل الملكية", interaction.channel, "transfer", interaction.message))

    @discord.ui.button(label="حذف الروم", emoji="🗑️", style=discord.ButtonStyle.danger, custom_id="tv_delete", row=2)
    async def delete_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.owner_check(interaction):
            return
        channel = interaction.channel
        rooms_data.pop(str(channel.id), None)
        save_rooms()
        # حذف مباشر بدون رسالة تأخير منفصلة — رد واحد فقط يوضح إنه جاري الحذف
        await interaction.response.edit_message(content="🗑️ جاري حذف الروم...", embed=None, view=None)
        try:
            await channel.delete(reason="حذف بواسطة صاحب الروم")
        except discord.NotFound:
            pass


# ---------------------------------------------------------------------------
# أوامر السلاش
# ---------------------------------------------------------------------------

@bot.tree.command(name="setup", description="إعداد نظام الرومات المؤقتة (للإدمن فقط)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    category_name="اسم الكاتيجوري التي ستُنشأ",
    channel_name="اسم روم الدخول لإنشاء روم جديد",
)
async def setup_cmd(
    interaction: discord.Interaction,
    category_name: str = "🔊 الرومات المؤقتة",
    channel_name: str = "➕ إنشاء روم",
):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ هذا الأمر متاح للإدمن فقط", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    category = await guild.create_category(category_name)
    create_channel = await guild.create_voice_channel(channel_name, category=category)

    config_data[str(guild.id)] = {
        "category_id": category.id,
        "create_channel_id": create_channel.id,
    }
    save_config()

    embed = discord.Embed(
        title="✅ تم إعداد نظام الرومات المؤقتة بنجاح",
        description=(
            f"**الكاتيجوري:** {category.name}\n"
            f"**روم الإنشاء:** {create_channel.mention}\n\n"
            f"أي عضو يدخل روم **{channel_name}** سيحصل تلقائياً على روم خاص به "
            f"مع لوحة تحكم كاملة."
        ),
        color=discord.Color.green(),
    )
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="setup-remove", description="إلغاء نظام الرومات المؤقتة من هذا السيرفر (للإدمن فقط)")
@app_commands.default_permissions(administrator=True)
async def setup_remove_cmd(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ هذا الأمر متاح للإدمن فقط", ephemeral=True)
        return

    guild_id = str(interaction.guild.id)
    if guild_id not in config_data:
        await interaction.response.send_message("⚠️ لا يوجد إعداد لهذا السيرفر أصلاً", ephemeral=True)
        return

    config_data.pop(guild_id, None)
    save_config()
    await interaction.response.send_message("🗑️ تم إلغاء إعداد نظام الرومات المؤقتة", ephemeral=True)


# ---------------------------------------------------------------------------
# الأحداث
# ---------------------------------------------------------------------------

@bot.event
async def on_ready():
    bot.add_view(ControlPanelView())  # لتفعيل الأزرار حتى بعد إعادة تشغيل البوت
    try:
        synced = await bot.tree.sync()
        print(f"تم تسجيل {len(synced)} أمر سلاش")
    except Exception as e:
        print(f"خطأ أثناء مزامنة الأوامر: {e}")
    print(f"✅ تم تسجيل الدخول باسم: {bot.user}")


@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
):
    guild = member.guild
    guild_config = get_guild_config(guild.id)
    if guild_config is None:
        return

    create_channel_id = guild_config["create_channel_id"]
    category_id = guild_config["category_id"]

    # 1) العضو دخل روم "إنشاء روم"
    if after.channel and after.channel.id == create_channel_id:
        # لو العضو يملك رومًا بالفعل، ننقله له بدل إنشاء روم جديد (أخف على السيرفر)
        existing_id = find_owned_room(guild.id, member.id)
        if existing_id:
            existing_channel = guild.get_channel(int(existing_id))
            if existing_channel:
                try:
                    await member.move_to(existing_channel, reason="لديه روم بالفعل")
                except discord.HTTPException:
                    pass
                return
            else:
                # الروم كان محذوف لكن السجل بقي، ننظفه
                rooms_data.pop(existing_id, None)
                save_rooms()

        async with creation_lock:
            # تحقق ثانٍ بعد أخذ القفل (تحسباً لوصول الحدث مرتين بسرعة)
            if find_owned_room(guild.id, member.id):
                return

            category = guild.get_channel(category_id)

            # صلاحيات صريحة: الروم يظهر ويمكن دخوله من الجميع، بينما صاحب الروم
            # يحصل على صلاحيات إدارة كاملة (تحريك/كتم/تغيير) داخل ديسكورد نفسه
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=True, connect=True),
                member: discord.PermissionOverwrite(
                    view_channel=True,
                    connect=True,
                    manage_channels=True,
                    move_members=True,
                    mute_members=True,
                    deafen_members=True,
                    priority_speaker=True,
                ),
                guild.me: discord.PermissionOverwrite(
                    view_channel=True, connect=True, manage_channels=True, move_members=True
                ),
            }

            new_channel = await guild.create_voice_channel(
                name=f"🔊 روم {member.display_name}"[:90],
                category=category,
                overwrites=overwrites,
            )

            try:
                await member.move_to(new_channel, reason="إنشاء روم مؤقت")
            except discord.HTTPException:
                await new_channel.delete(reason="فشل نقل العضو")
                return

            rooms_data[str(new_channel.id)] = {
                "owner_id": member.id,
                "guild_id": guild.id,
                "locked": False,
            }
            save_rooms()

            embed = build_panel_embed(new_channel, member, locked=False)
            try:
                panel_msg = await new_channel.send(
                    content=f"مرحباً {member.mention} 👋 هذا رومك الخاص!",
                    embed=embed,
                    view=ControlPanelView(),
                )
                rooms_data[str(new_channel.id)]["panel_message_id"] = panel_msg.id
                save_rooms()
            except discord.Forbidden:
                pass

    # 2) العضو غادر روماً مؤقتاً
    if before.channel and before.channel.id != create_channel_id:
        room = get_room(before.channel.id)
        if room:
            if len(before.channel.members) == 0:
                # الروم أصبح فارغاً بالكامل -> يُحذف
                rooms_data.pop(str(before.channel.id), None)
                save_rooms()
                try:
                    await before.channel.delete(reason="الروم أصبح فارغاً")
                except discord.NotFound:
                    pass

            elif room["owner_id"] == member.id:
                # المالك غادر لكن لا يزال هناك أعضاء -> نقل ملكية تلقائي حتى لا يبقى الروم بلا إدارة
                new_owner = before.channel.members[0]
                room["owner_id"] = new_owner.id
                save_rooms()

                try:
                    await before.channel.set_permissions(member, overwrite=None)
                    new_overwrite = discord.PermissionOverwrite(
                        view_channel=True,
                        connect=True,
                        manage_channels=True,
                        move_members=True,
                        mute_members=True,
                        deafen_members=True,
                        priority_speaker=True,
                    )
                    await before.channel.set_permissions(new_owner, overwrite=new_overwrite)
                except discord.Forbidden:
                    pass

                await refresh_panel(before.channel)
                try:
                    await before.channel.send(
                        f"👑 تم نقل ملكية الروم تلقائياً إلى {new_owner.mention} بعد مغادرة المالك السابق."
                    )
                except discord.Forbidden:
                    pass


# ---------------------------------------------------------------------------
# تشغيل البوت
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit(
            "❌ لم يتم العثور على DISCORD_TOKEN. أنشئ ملف .env وضع التوكن بداخله (انظر ملف .env.example)."
        )
    bot.run(TOKEN)
