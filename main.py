import discord
from discord import app_commands
from discord.ext import commands

from cerebras.cloud.sdk import AsyncCerebras

import asyncpg
import os
import io
import re
import sys
import json
import httpx
import traceback
import asyncio
import time

from datetime import datetime, timezone, timedelta

from rapidfuzz import fuzz

# =========================================================
# CONFIG
# =========================================================

OWNER_ID = 1431956941315510437
ALLOWED_GUILD_ID = 1499426920603848787

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CEREBRAS_API_KEY = os.getenv("CEREBRAS_API_KEY")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

MAX_FILE_SIZE = 500_000
MAX_CONTEXT_LINES = 5500


# =========================================================
# SECURITY
# =========================================================

BLOCKED_PROMPT_TERMS = [
    "ignore previous instructions",
    "reveal system prompt",
    "show api key",
    "developer message",
    "bypass restrictions",
    "system prompt",
    "environment variables",
]


def is_safe_prompt(text: str):

    text = text.lower()

    for term in BLOCKED_PROMPT_TERMS:

        if term in text:
            return False

    return True

# =========================================================
# HTTPX PATCH
# =========================================================

_original_httpx_init = httpx.Client.__init__


def _patched_httpx_init(self, *args, **kwargs):

    kwargs.pop("proxy", None)

    _original_httpx_init(
        self,
        *args,
        **kwargs
    )


httpx.Client.__init__ = _patched_httpx_init

_original_httpx_async_init = httpx.AsyncClient.__init__


def _patched_httpx_async_init(self, *args, **kwargs):

    kwargs.pop("proxy", None)

    _original_httpx_async_init(
        self,
        *args,
        **kwargs
    )


httpx.AsyncClient.__init__ = _patched_httpx_async_init

# =========================================================
# AUDIOOP FIX
# =========================================================

try:
    import audioop

except ImportError:

    import audioop_lts as audioop
    sys.modules["audioop"] = audioop

# =========================================================
# ENV VALIDATION
# =========================================================

REQUIRED_ENV_VARS = {
    "DISCORD_TOKEN": DISCORD_TOKEN,
    "CEREBRAS_API_KEY": CEREBRAS_API_KEY,
    "OPENROUTER_API_KEY": OPENROUTER_API_KEY,
    "DATABASE_URL": DATABASE_URL,
}

missing_env = [
    key for key, value in REQUIRED_ENV_VARS.items()
    if not value
]

if missing_env:

    raise RuntimeError(
        f"Missing environment variables: {', '.join(missing_env)}"
    )

# =========================================================
# DATABASE
# =========================================================

db_pool = None


async def init_db():

    global db_pool

    db_pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=1,
        max_size=10
    )

    async with db_pool.acquire() as conn:

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                coins BIGINT NOT NULL DEFAULT 0
            )
        """)

        



        await conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_rewards (
                user_id BIGINT PRIMARY KEY,
                last_claim TIMESTAMPTZ
            )
        """)

# =========================================================
# CLIENTS
# =========================================================

cerebras_client = AsyncCerebras(
    api_key=CEREBRAS_API_KEY
)

# =========================================================
# MODELS
# =========================================================

MODEL_PRIORITY = [
    "openai/gpt-oss-120b:free",
    "zai-glm-4.7",
    "llama3.1-8b"
]

BAD_MODELS = set()

SYSTEM_PROMPT_CREATE = """
You are an expert Roblox Luau developer.

Return ONLY raw Luau code.
No markdown.
No explanations.
"""

SYSTEM_PROMPT_EDIT = """
You are an expert Roblox Luau developer.

Return ONLY the FULL edited Luau file.

Rules:
- No markdown
- No explanations
- Return complete final file
- Keep all unrelated code unchanged
"""

CASH = "<:cash:1499803753396703252>"

EMBED_COLOR = 0x7B14BB

EMBED_IMAGE = (
    "https://cdn.discordapp.com/attachments/"
    "1432016228049752135/"
    "1502927683560931408/"
    "file_00000000d3807246aa64c785bc8dd663.png"
)


def themed_embed(**kwargs):

    embed = discord.Embed(
        color=EMBED_COLOR,
        **kwargs
    )

    embed.set_image(
        url=EMBED_IMAGE
    )

    return embed

# =========================================================
# USER LOCKS
# =========================================================

USER_LOCKS = {}


def get_user_lock(user_id: int):

    if user_id not in USER_LOCKS:

        USER_LOCKS[user_id] = asyncio.Lock()

    return USER_LOCKS[user_id]

# =========================================================
# ERROR LOGGING
# =========================================================


async def log_error(message: str):

    print(f"🚨 ERROR:\n{message}")

    if not WEBHOOK_URL:
        return

    try:

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30)
        ) as client:

            await client.post(
                WEBHOOK_URL,
                json={
                    "content":
                        f"```{message[:1900]}```"
                }
            )

    except Exception as e:

        print(f"Webhook failed: {e}")

# =========================================================
# OWNER CHECK
# =========================================================


def is_owner(interaction_or_ctx):

    user = (
        interaction_or_ctx.user
        if hasattr(interaction_or_ctx, "user")
        else interaction_or_ctx.author
    )

    return user.id == OWNER_ID

# =========================================================
# CHANNEL CHECK
# =========================================================


def can_use_script_commands(
    interaction: discord.Interaction
):

    if interaction.guild is None:
        return False

    if interaction.guild.id != ALLOWED_GUILD_ID:
        return False

    expected_channel = (
        f"🚀・{interaction.user.name}"
        .lower()
    )

    if interaction.channel.name != expected_channel:
        return False

    overwrites = interaction.channel.overwrites_for(
        interaction.user
    )

    return (
        overwrites.view_channel is True
        and overwrites.use_application_commands is True
    )

# =========================================================
# DATABASE FUNCTIONS
# =========================================================


async def get_user_coins(user_id: int):

    async with db_pool.acquire() as conn:

        row = await conn.fetchrow(
            "SELECT coins FROM users WHERE user_id = $1",
            user_id
        )

        if not row:

            await conn.execute(
                """
                INSERT INTO users (user_id, coins)
                VALUES ($1, 0)
                """,
                user_id
            )

            return 0

        return row["coins"]


async def update_user_coins(
    user_id: int,
    amount: int
):

    async with db_pool.acquire() as conn:

        row = await conn.fetchrow(
            "SELECT coins FROM users WHERE user_id = $1",
            user_id
        )

        if not row:

            new_total = max(0, amount)

            await conn.execute(
                """
                INSERT INTO users (user_id, coins)
                VALUES ($1, $2)
                """,
                user_id,
                new_total
            )

            return new_total

        current = row["coins"]

        new_total = max(
            0,
            current + amount
        )

        await conn.execute(
            """
            UPDATE users
            SET coins = $1
            WHERE user_id = $2
            """,
            new_total,
            user_id
        )

        return new_total


async def get_status_data(user_id: int):

    async with db_pool.acquire() as conn:

        row = await conn.fetchrow(
            """
            SELECT *
            FROM status_rewards
            WHERE user_id = $1
            """,
            user_id
        )

        if not row:

            return {
                "hour": 0,
                "day": 0,
                "week": 0,
                "total": 0
            }

        return {
            "hour": row["hour_coins"],
            "day": row["day_coins"],
            "week": row["week_coins"],
            "total": row["total_coins"]
        }

# =========================================================
# CLEANERS
# =========================================================


def clean_code(text: str):

    if not text:
        return ""

    text = text.strip()

    if text.startswith("```"):

        text = "\n".join(
            text.split("\n")[1:-1]
        )

    return text.strip()

# =========================================================
# STRUCTURE MAP
# =========================================================


def build_structure_map(content: str):

    structure = []

    lines = content.splitlines()

    patterns = [
        r"local function\s+([A-Za-z0-9_]+)",
        r"function\s+([A-Za-z0-9_\.]+)",
        r"([A-Za-z0-9_]+)\s*=\s*function"
    ]

    for i, line in enumerate(lines):

        for pattern in patterns:

            match = re.search(
                pattern,
                line
            )

            if match:

                structure.append({
                    "line": i + 1,
                    "name": match.group(1),
                    "preview": line.strip()
                })

    return structure

# =========================================================
# RETRIEVAL
# =========================================================


def retrieve_relevant_chunks(
    content: str,
    instructions: str
):

    lines = content.splitlines()

    keywords = re.findall(
        r"[A-Za-z_]+",
        instructions.lower()
    )

    scored = []

    for i, line in enumerate(lines):

        score = 0
        lower = line.lower()

        for keyword in keywords:

            if keyword in lower:
                score += 5
            score += (
                fuzz.partial_ratio(
                    keyword,
                    lower
                ) / 100
            )

        if score > 2:
            scored.append((i, score))

    scored.sort(
        key=lambda x: x[1],
        reverse=True
    )

    selected = set()

    for i in range(
        min(80, len(lines))
    ):
        selected.add(i)

    for line_index, _ in scored[:20]:

        start = max(
            0,
            line_index - 25
        )
        end = min(
            len(lines),
            line_index + 25
        )

        for i in range(start, end):
            selected.add(i)

    selected = sorted(selected)

    output = []

    for i in selected:

        output.append(
            f"{i+1}: {lines[i]}"
        )

    return "\n".join(
        output[:MAX_CONTEXT_LINES]
    )

# =========================================================
# AI
# =========================================================


async def get_ai_completion(
    messages,
    max_tokens=4000
):

    last_error = "Unknown"

    for model_index, model_id in enumerate(MODEL_PRIORITY, start=1):

        if model_id in BAD_MODELS:
            continue

        try:

            print(f"⚡ Trying model #{model_index}")

            if model_id.startswith("openai/"):

                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(60)
                ) as client:

                    res = await client.post(
                        "https://openrouter.ai/api/v1/chat/completions",

                        headers={
                            "Authorization":
                                f"Bearer {OPENROUTER_API_KEY}",

                            "Content-Type":
                                "application/json"
                        },

                        json={
                            "model": model_id,
                            "messages": messages,
                            "temperature": 0.1,
                            "max_tokens": max_tokens
                        }
                    )

                    if res.status_code != 200:

                        raise Exception(
                            f"HTTP {res.status_code}: {res.text}"
                        )

                    data = res.json()

                    if "choices" in data:

                        usage = data.get("usage", {})
                        return (
                            data["choices"][0]["message"]["content"],
                            model_index,
                            usage.get("total_tokens", 0)
                        )

                    raise Exception(str(data))

            else:

                response = (
                    await cerebras_client.chat.completions.create(
                        model=model_id,
                        messages=messages,
                        max_tokens=max_tokens,
                        temperature=0.1
                    )
                )

                if response and response.choices:

                    token_count = 0
                    if hasattr(response, "usage") and response.usage:
                        token_count = getattr(
                            response.usage,
                            "total_tokens",
                            0
                        )

                    return (
                        response.choices[0].message.content,
                        model_index,
                        token_count
                    )

        except Exception as e:

            err = str(e)
            last_error = err
            await log_error(
                f"{model_id} failed:\n{err}"
            )
            if "404" in err:
                BAD_MODELS.add(model_id)

    await log_error(
        f"ALL MODELS FAILED:\n{last_error}"
    )

    return "-- AI FAILED --", 0, 0

# =========================================================
# BOT
# =========================================================


class ScriptBot(commands.Bot):

    def __init__(self):

        intents = discord.Intents.default()

        intents.message_content = True
        intents.guilds = True
        intents.members = True
        intents.presences = True

        super().__init__(
            command_prefix="!",
            intents=intents
        )

    async def setup_hook(self):

        print(
            f"✅ Logged in as {self.user}"
        )

        await init_db()

        

        for guild in self.guilds:

            if guild.id != ALLOWED_GUILD_ID:

                try:
                    await guild.leave()

                except:
                    pass

    
    bot = ScriptBot()

# =========================================================
# GLOBAL ERROR HANDLER
# =========================================================


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error
):

    if isinstance(
        error,
        app_commands.CommandOnCooldown
    ):

        return await interaction.response.send_message(
            (
                f"⏳ Cooldown active. "
                f"Try again in "
                f"{error.retry_after:.1f}s"
            ),
            ephemeral=True
        )

    await log_error(str(error))

# =========================================================
# OWNER COMMANDS
# =========================================================


@bot.command(name="sync")
async def sync(ctx):

    if not is_owner(ctx):
        return

    synced = await bot.tree.sync()

    await ctx.send(
        f"✅ Synced {len(synced)} commands."
    )


@bot.command(name="status_info")
async def status_info(ctx):

    if not is_owner(ctx):
        return

    embed = discord.Embed(
        title="<a:global:1504083730980143204> Status Rewards",
        color=0x7B14BB
    )

    embed.description = (
        "# <a:gift:1504084446683336826> Earn Free Coins\n\n"
        "<a:discord:1504084265414033408> Add this to your Discord status:\n\n"
        f"```{STATUS_REWARD_TEXT}```\n\n"
        f"> <a:money:1504084340248936572> Earn `{STATUS_REWARD}` coins every `{STATUS_INTERVAL}` seconds while online. (1 Coin per Hour)\n"
        "> <a:question:1504084305067114629> Use `/status` to check earnings.\n"
        "> <a:tickmark:1504083668606648422> Run `/status` anytime to see your hourly, daily, weekly, and total free status earnings.\n"
    )

    embed.set_image(
        url="https://cdn.discordapp.com/attachments/1432016228049752135/1502927683560931408/file_00000000d3807246aa64c785bc8dd663.png"
    )

    await ctx.send(
        embed=embed
    )


@bot.command(name="purchase")
async def purchase(ctx):

    if not is_owner(ctx):
        return

    embed = discord.Embed(
        title="<a:purchase:1504084364420452352> Coins System",
        color=0x7B14BB
    )

    embed.description = (
        "# <:cash:1499803753396703252> Coins System\n\n"

        "## <a:book:1504084400433008720> How to Use\n\n"

        "### <a:computer:1504084284435202098> /create_script\n"
        "> <a:moneyman:1504084503172350084> Cost: 3 Coins\n"
        "> <a:tickmark:1504083668606648422> Generates a complete script from scratch. When using this command, please ensure you explain every detail, including specific features, intended usage, UI preferences, and coding style.\n\n"

        "### <a:computer:1504084284435202098> /edit_script\n"
        "> <a:moneyman:1504084503172350084> Cost: 2 Coins\n"
        "> <a:tickmark:1504083668606648422> Use this to edit existing code, fix bugs, add/remove features, or refine existing sources.\n\n"

        "## <a:money:1504084340248936572> Pricing (per 20 Coins)\n\n"

        "<:Paypal:1499819080037957653> PayPal: 2.49€\n"
        "<:Roblox:1499818898688704553> Robux: 715 R$\n"
        "<:BrainRot:1499819229900308550> <:gram:1499827233462947860> Other: Equal Value\n\n"

        "## <:notification:1504083713548357732> Information\n\n"

        "> <a:discordv2:1504084489683341353> Privacy Guaranteed: The script will be sent to your DMs even though you use the message on a public channel.\n"
        "> <a:question:1504084305067114629> This ensures your custom code and scripts remain confidential.\n\n"

        "## <a:waves:1504084476198916206> Ready to buy?\n"
        "> <a:arrow:1504084325006708776> Scroll up to open a ticket!"
    )

    embed.set_image(
        url="https://cdn.discordapp.com/attachments/1432016228049752135/1502927683560931408/file_00000000d3807246aa64c785bc8dd663.png"
    )

    await ctx.send(
        embed=embed
    )


@bot.command(name="guild_leave")
async def guild_leave(ctx):

    if not is_owner(ctx):
        return

    report = []

    for guild in bot.guilds:

        if guild.id == ALLOWED_GUILD_ID:
            continue

        try:
            owner = guild.owner

        except:
            owner = "Unknown"

        report.append(
            f"""
━━━━━━━━━━━━━━━━━━
Guild Name: {guild.name}
Guild ID: {guild.id}
Owner: {owner}
Members: {guild.member_count}
Channels: {len(guild.channels)}
Roles: {len(guild.roles)}
Created: {guild.created_at}
━━━━━━━━━━━━━━━━━━
"""
        )

    chunks = []

    current = ""
    for entry in report:

        if len(current) + len(entry) > 1900:

            chunks.append(current)
            current = entry
        else:
            current += entry

    if current:
        chunks.append(current)

    for chunk in chunks:

        await ctx.send(
            f"```{chunk}```"
        )

    for guild in bot.guilds:

        if guild.id != ALLOWED_GUILD_ID:

            try:
                await guild.leave()

            except Exception as e:

                await ctx.send(
                    f"Failed leaving {guild.name}: {e}"
                )

# =========================================================
# CHANNEL COMMANDS
# =========================================================


@bot.tree.command(name="create_channel", description="Create a private scripting channel for a user")
async def create_channel(
    interaction: discord.Interaction,
    user: discord.Member
):

    if not is_owner(interaction):

        return await interaction.response.send_message(
            "# <:emoji_22:1504083784033763428> Access Denied\n"
            "> You cannot use this command.",
            ephemeral=True
        )

    channel_name = (
        f"🚀・{user.name}"
        .lower()
    )

    existing = discord.utils.get(
        interaction.guild.channels,
        name=channel_name
    )

    if existing:

        return await interaction.response.send_message(
            f"## <a:question:1504084305067114629> Channel Exists\n"
            f"> {existing.mention}",
            ephemeral=True
        )

    overwrites = {

        interaction.guild.default_role:
            discord.PermissionOverwrite(
                view_channel=False
            ),

        user:
            discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                use_application_commands=True
            ),

        interaction.guild.me:
            discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                manage_channels=True,
                use_application_commands=True
            )
    }

    channel = await interaction.guild.create_text_channel(
        name=channel_name,
        overwrites=overwrites
    )

    await channel.send(
        f"# <:Roblox:1499818898688704553> Welcome {user.mention}\n"
        f"> Your private scripting channel is ready.\n"
        f"> Only you can access and use commands here.\n\n"
        f"<a:arrow:1504084325006708776> Start with `/create_script`"
    )

    await interaction.response.send_message(
        f"## <a:tickmark:1504083668606648422> Channel Created\n"
        f"> {channel.mention}",
        ephemeral=True
    )


@bot.tree.command(name="delete_channel", description="Delete a user private scripting channel")
async def delete_channel(
    interaction: discord.Interaction,
    user: discord.Member
):

    if not is_owner(interaction):

        return await interaction.response.send_message(
            "# <:emoji_22:1504083784033763428> Access Denied\n"
            "> You cannot use this command.",
            ephemeral=True
        )

    channel_name = (
        f"🚀・{user.name}"
        .lower()
    )

    channel = discord.utils.get(
        interaction.guild.channels,
        name=channel_name
    )

    if not channel:

        return await interaction.response.send_message(
            "## <a:question:1504084305067114629> Channel Missing\n"
            "> Could not find that channel.",
            ephemeral=True
        )

    await channel.delete()

    await interaction.response.send_message(
        f"## <a:X_:1504083693126422650> Channel Deleted\n"
        f"> Removed `{channel_name}`",
        ephemeral=True
    )

# =========================================================
# COINS
# =========================================================


@bot.tree.command(name="coin_add", description="Add coins to a user balance")
async def coin_add(
    interaction: discord.Interaction,
    user: discord.Member,
    amount: int
):

    if not is_owner(interaction):

        return await interaction.response.send_message(
            "❌ No permission",
            ephemeral=True
        )

    new_bal = await update_user_coins(
        user.id,
        amount
    )

    embed = themed_embed(
        title="<a:moneyman:1504084503172350084> Coins Added",
        description=(
            f"> User: {user.mention}\n"
            f"> New balance: {new_bal} {CASH}"
        )
    )

    await interaction.response.send_message(
        embed=embed
    )


@bot.tree.command(name="coin_remove", description="Remove coins from a user balance")
async def coin_remove(
    interaction: discord.Interaction,
    user: discord.Member,
    amount: int
):

    if not is_owner(interaction):

        return await interaction.response.send_message(
            "❌ No permission",
            ephemeral=True
        )

    new_bal = await update_user_coins(
        user.id,
        -amount
    )

    embed = themed_embed(
        title="<a:money:1504084340248936572> Coins Removed",
        description=(
            f"> User: {user.mention}\n"
            f"> New balance: {new_bal} {CASH}"
        )
    )

    await interaction.response.send_message(
        embed=embed
    )

# =========================================================
# USER
# =========================================================


@bot.tree.command(name="balance", description="Check your coin balance")
@app_commands.checks.cooldown(1, 3)
async def balance(
    interaction: discord.Interaction
):

    coins = await get_user_coins(
        interaction.user.id
    )

    embed = themed_embed(
        title="<a:gift:1504084446683336826> Balance",
        description=f"> {coins} {CASH}"
    )

    await interaction.response.send_message(
        embed=embed
    )


@bot.tree.command(name="status", description="View your status reward statistics")
@app_commands.checks.cooldown(1, 3)
async def status(interaction: discord.Interaction):

    data = await get_status_data(interaction.user.id)

    embed = discord.Embed(
        title="💜 Status Reward Earnings",
        color=0x7B14BB
    )

    embed.description = (
        "## Free Coins from Status\n"
        f"> This hour: **{data['hour']:.0f}** {CASH}\n"
        f"> Today: **{data['day']:.0f}** {CASH}\n"
        f"> This week: **{data['week']:.0f}** {CASH}\n"
        f"> Total: **{data['total']:.0f}** {CASH}\n\n"
        f"> Required status: `{STATUS_REWARD_TEXT}`"
    )

    await interaction.response.send_message(embed=embed)

# =========================================================
# CREATE SCRIPT
# =========================================================


@bot.tree.command(name="create_script", description="Generate a Roblox Luau script with AI")
@app_commands.checks.cooldown(1, 15)
async def create_script(
    interaction: discord.Interaction,
    prompt: str
):

    if not can_use_script_commands(interaction):

        return await interaction.response.send_message(
            (
                "# <:emoji_22:1504083784033763428> Wrong Channel\n"
                "> You must use commands inside your private channel."
            ),
            ephemeral=True
        )

    if not is_safe_prompt(prompt):

        return await interaction.response.send_message(
            "❌ Unsafe prompt detected.",
            ephemeral=True
        )

    lock = get_user_lock(
        interaction.user.id
    )

    async with lock:

        if await get_user_coins(
            interaction.user.id
        ) < 3:

            return await interaction.response.send_message(
                "## <:cash:1499803753396703252> Not Enough Coins\n"
                "> Need 3 coins.",
                ephemeral=True
            )

        await update_user_coins(
            interaction.user.id,
            -3
        )

        await interaction.response.defer()

        start_time = time.perf_counter()

        result, model_number, tokens_used = await get_ai_completion(
            [
                {
                    "role": "system",
                    "content":
                        SYSTEM_PROMPT_CREATE
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            max_tokens=5000
        )

        result = clean_code(result)

        elapsed = round(
            time.perf_counter() - start_time,
            2
        )

        file = discord.File(
            io.BytesIO(result.encode()),
            filename="script.lua"
        )

        dm_embed = themed_embed(
            title="<a:computer:1504084284435202098> Script Generated",
            description=(
                f"> ⏱️ {elapsed}s\n"
                f"> 🤖 Model #{model_number}\n"
                f"> 🧠 {tokens_used} tokens"
            )
        )

        await interaction.user.send(
            embed=dm_embed,
            file=file
        )

        public_embed = themed_embed(
            title="<a:tickmark:1504083668606648422> Script Sent",
            description=(
                "> Your generated script has been sent to your DMs."
            )
        )

        await interaction.followup.send(
            embed=public_embed
        )

# =========================================================
# EDIT SCRIPT
# =========================================================


@bot.tree.command(name="edit_script", description="Edit or improve an existing Roblox Luau script")
@app_commands.checks.cooldown(1, 10)
async def edit_script(
    interaction: discord.Interaction,
    file: discord.Attachment,
    instructions: str
):

    if not can_use_script_commands(interaction):

        return await interaction.response.send_message(
            (
                "# <:emoji_22:1504083784033763428> Wrong Channel\n"
                "> You must use commands inside your private channel."
            ),
            ephemeral=True
        )

    if not is_safe_prompt(instructions):

        return await interaction.response.send_message(
            "❌ Unsafe instructions detected.",
            ephemeral=True
        )

    if file.size > MAX_FILE_SIZE:

        return await interaction.response.send_message(
            "❌ File too large.",
            ephemeral=True
        )

    lock = get_user_lock(
        interaction.user.id
    )

    async with lock:

        if await get_user_coins(
            interaction.user.id
        ) < 2:

            return await interaction.response.send_message(
                "## <:cash:1499803753396703252> Not Enough Coins\n"
                "> Need 2 coins.",
                ephemeral=True
            )

        await update_user_coins(
            interaction.user.id,
            -2
        )

        await interaction.response.defer()

        try:

            original_content = (
                await file.read()
            ).decode("utf-8")

        except UnicodeDecodeError:

            return await interaction.followup.send(
                "❌ File must be UTF-8 encoded."
            )

        structure_map = build_structure_map(
            original_content
        )

        relevant_context = retrieve_relevant_chunks(
            original_content,
            instructions
        )

        prompt = f"""
INSTRUCTIONS:
{instructions}

STRUCTURE MAP:
{json.dumps(structure_map[:50], indent=2)}

RELEVANT CONTEXT:
{relevant_context}

FULL ORIGINAL FILE:
{original_content}
"""

        start_time = time.perf_counter()

        edited_content, model_number, tokens_used = await get_ai_completion(
            [
                {
                    "role": "system",
                    "content":
                        SYSTEM_PROMPT_EDIT
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            max_tokens=7000
        )

        edited_content = clean_code(
            edited_content
        )

        elapsed = round(
            time.perf_counter() - start_time,
            2
        )

        out_file = discord.File(
            io.BytesIO(
                edited_content.encode()
            ),
            filename="edited.lua"
        )

        dm_embed = themed_embed(
            title="<a:book:1504084400433008720> Script Edited",
            description=(
                f"> ⏱️ {elapsed}s\n"
                f"> 🤖 Model #{model_number}\n"
                f"> 🧠 {tokens_used} tokens"
            )
        )

        await interaction.user.send(
            embed=dm_embed,
            file=out_file
        )

        public_embed = themed_embed(
            title="<a:tickmark:1504083668606648422> Script Sent",
            description=(
                "> Your edited script has been sent to your DMs."
            )
        )

        await interaction.followup.send(
            embed=public_embed
        )

# =========================================================
# ERROR EVENT
# =========================================================


@bot.event
async def on_error(event, *args, **kwargs):

    err = traceback.format_exc()

    await log_error(
        f"{event}:\n{err}"
    )

# =========================================================
# RUN
# =========================================================



@bot.tree.command(
    name="daily",
    description="Claim your daily reward"
)
async def daily(
    interaction: discord.Interaction
):

    user_id = interaction.user.id

    now = datetime.now(
        timezone.utc
    )

    async with db_pool.acquire() as conn:

        row = await conn.fetchrow(
            """
            SELECT last_claim
            FROM daily_rewards
            WHERE user_id = $1
            """,
            user_id
        )

        if row and row["last_claim"]:

            last_claim = row["last_claim"]

            if last_claim.tzinfo is None:

                last_claim = last_claim.replace(
                    tzinfo=timezone.utc
                )

            elapsed = (
                now - last_claim
            ).total_seconds()

            if elapsed < 86400:

                remaining = int(
                    86400 - elapsed
                )

                hours = remaining // 3600
                minutes = (
                    remaining % 3600
                ) // 60

                embed = discord.Embed(
                    description=(
                        f"<:notification:1504083713548357732> "
                        f"You already claimed your "
                        f"<a:gift:1504084446683336826> "
                        f"daily reward!\n\n"
                        f"<a:question:1504084305067114629> "
                        f"Try again in "
                        f"`{hours}h {minutes}m`"
                    ),
                    color=0xffcc00
                )

                return await interaction.response.send_message(
                    embed=embed,
                    ephemeral=True
                )

        reward = 1

        await update_user_coins(
            user_id,
            reward
        )

        await conn.execute(
            """
            INSERT INTO daily_rewards (
                user_id,
                last_claim
            )
            VALUES ($1, $2)
            ON CONFLICT (user_id)
            DO UPDATE SET
            last_claim = EXCLUDED.last_claim
            """,
            user_id,
            now
        )

        balance = await get_user_coins(
            user_id
        )

        embed = discord.Embed(
            description=(
                f"<a:gift:1504084446683336826> "
                f"You claimed your daily reward!\n\n"
                f"<:cash:1499803753396703252> "
                f"Reward: `1.00` coins\n"
                f"<a:moneyman:1504084503172350084> "
                f"Balance: `{balance:.0f}` coins\n\n"
                f"<a:tickmark:1504083668606648422> "
                f"Come back tomorrow for more!"
            ),
            color=0x57F287
        )

        await interaction.response.send_message(
            embed=embed
        )



bot.run(DISCORD_TOKEN)
