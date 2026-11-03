"""
Muteboard – Discord-Bot, der misst, wer wie lange im Voice-Channel stumm bzw.
taub geschaltet ist, und jede Woche vier Leaderboards postet:

  • Full Mute  – selbst taub (Kopfhörer aus)
  • Nur Mute   – selbst stumm (Mikro aus), aber nicht taub
  • Gesamt     – beides zusammen
  • Prozent    – Anteil der Voice-Zeit, die jemand stumm/taub war

Die Sieger bekommen optional automatisch eine Rolle, die jede Woche weiterwandert.
"""
import datetime as dt
import logging
import os
import sqlite3
import time
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

load_dotenv()
discord.utils.setup_logging(root=True)  # früh, damit auch Start-Meldungen im Log landen
log = logging.getLogger("muteboard")


def _id(name: str) -> int:
    v = os.getenv(name, "").strip()
    return int(v) if v.isdigit() else 0


TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
CHANNEL_ID = _id("LEADERBOARD_CHANNEL_ID")
if not TOKEN or not CHANNEL_ID:
    raise SystemExit("Bitte DISCORD_TOKEN und LEADERBOARD_CHANNEL_ID in der .env eintragen.")
TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Berlin"))
POST_WEEKDAY = int(os.getenv("POST_WEEKDAY", "6"))  # 0 = Montag … 6 = Sonntag
POST_HOUR = int(os.getenv("POST_HOUR", "20"))
POST_MINUTE = int(os.getenv("POST_MINUTE", "0"))
IGNORE_AFK = os.getenv("IGNORE_AFK_CHANNEL", "1") == "1"
DB_PATH = os.getenv("DB_PATH", "muteboard.db")
TOP_N = int(os.getenv("TOP_N", "0"))  # 0 = alle anzeigen
LIMIT = TOP_N if TOP_N > 0 else -1  # SQLite: LIMIT -1 = unbegrenzt
EMBED_MAX = 4096                     # Discord-Limit pro Embed-Beschreibung
MESSAGE_EMBEDS_MAX = 6000            # Discord-Limit für alle Embeds einer Nachricht

DEAF, MUTE, ALL, PCT, VOICE = "deaf", "mute", "all", "pct", "voice"

# Prozent-Rangliste: was zählt als "stumm" und wie viel Voice-Zeit braucht man mindestens
PERCENT_BASIS = os.getenv("PERCENT_BASIS", ALL)          # "all" oder "deaf"
PERCENT_MIN_MINUTES = int(os.getenv("PERCENT_MIN_MINUTES", "120"))

# Rollen-IDs für die Sieger (leer = keine Rolle für diese Rangliste)
ROLE_IDS = {
    DEAF: _id("ROLE_DEAF_ID"),
    MUTE: _id("ROLE_MUTE_ID"),
    ALL: _id("ROLE_ALL_ID"),
    PCT: _id("ROLE_PERCENT_ID"),
}

BOARDS = {
    DEAF: dict(name="Full Mute", icon="🔇", colour=discord.Colour.dark_grey(),
               title_rank="Full Mute", empty="Niemand war full mute."),
    MUTE: dict(name="Nur Mute", icon="🎙️", colour=discord.Colour.blurple(),
               title_rank="Nur Mute", empty="Niemand hatte nur das Mikro aus."),
    ALL:  dict(name="Mute + Full Mute", icon="🤐", colour=discord.Colour.gold(),
               title_rank="Gesamt", empty="Niemand war stumm. Erstaunlich gesprächig hier."),
    PCT:  dict(name="Stumm-Quote", icon="📊", colour=discord.Colour.teal(),
               title_rank="Stumm-Quote",
               empty=f"Niemand war mindestens {PERCENT_MIN_MINUTES} min im Voice."),
}
MUTE_KINDS = (DEAF,) if PERCENT_BASIS == DEAF else (DEAF, MUTE)


# --------------------------------------------------------------------------- #
# Speicher
# --------------------------------------------------------------------------- #
class Store:
    """SQLite: Sekunden pro (Server, User, Periode, Art). Eine Periode = eine Woche.
    Arten: deaf, mute (stumm/taub) und voice (überhaupt im Voice)."""

    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS totals (
                guild_id INTEGER, user_id INTEGER, period INTEGER,
                kind TEXT NOT NULL, seconds REAL,
                PRIMARY KEY (guild_id, user_id, period, kind)
            );
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value REAL);
            CREATE TABLE IF NOT EXISTS role_holders (
                guild_id INTEGER, board TEXT, user_id INTEGER,
                PRIMARY KEY (guild_id, board)
            );
            INSERT OR IGNORE INTO meta VALUES ('period', 1);
            """
        )
        # Voice-Zeit wird erst ab dieser Version gemessen. Die Prozent-Rangliste
        # startet deshalb erst mit der nächsten vollständigen Woche, sonst wären
        # die Quoten der laufenden Woche verfälscht (Mute-Zeit ohne Voice-Zeit).
        if self.get_meta("pct_start") is None:
            has_data = self.db.execute(
                "SELECT 1 FROM totals WHERE period = ? LIMIT 1", (self.period,)).fetchone()
            self.set_meta("pct_start", self.period + 1 if has_data else self.period)
        self.db.commit()

    # --- Meta ---------------------------------------------------------------
    def get_meta(self, key: str):
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: float):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))
        self.db.commit()

    @property
    def period(self) -> int:
        return int(self.get_meta("period"))

    @property
    def pct_start(self) -> int:
        return int(self.get_meta("pct_start"))

    def next_period(self):
        self.set_meta("period", self.period + 1)

    # --- Zeiten -------------------------------------------------------------
    def add(self, guild_id: int, user_id: int, kind: str, seconds: float):
        if seconds <= 0:
            return
        self.db.execute(
            """INSERT INTO totals VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(guild_id, user_id, period, kind)
               DO UPDATE SET seconds = seconds + excluded.seconds""",
            (guild_id, user_id, self.period, kind, seconds),
        )
        self.db.commit()

    def top(self, guild_id: int, kind: str = DEAF, all_time: bool = False, n: int = LIMIT):
        """[(user_id, sekunden)] für deaf, mute oder all (= deaf + mute)."""
        kinds = (DEAF, MUTE) if kind == ALL else (kind,)
        where = f"guild_id = ? AND kind IN ({','.join('?' * len(kinds))})"
        args = [guild_id, *kinds]
        if not all_time:
            where += " AND period = ?"
            args.append(self.period)
        sql = f"""SELECT user_id, SUM(seconds) s FROM totals WHERE {where}
                  GROUP BY user_id ORDER BY s DESC LIMIT ?"""
        return self.db.execute(sql, (*args, n)).fetchall()

    def percent(self, guild_id: int, all_time: bool = False, n: int = LIMIT):
        """[(user_id, quote 0..1, stumm_sek, voice_sek)], nur mit Mindest-Voice-Zeit."""
        marks = ",".join("?" * len(MUTE_KINDS))
        where, args = "guild_id = ? AND period >= ?", [guild_id, self.pct_start]
        if not all_time:
            where += " AND period = ?"
            args.append(self.period)
        sql = f"""
            SELECT user_id,
                   SUM(CASE WHEN kind IN ({marks}) THEN seconds ELSE 0 END) AS m,
                   SUM(CASE WHEN kind = ? THEN seconds ELSE 0 END) AS v
            FROM totals WHERE {where}
            GROUP BY user_id HAVING v >= ?
            ORDER BY MIN(m / v, 1.0) DESC, v DESC LIMIT ?"""
        rows = self.db.execute(
            sql, (*MUTE_KINDS, VOICE, *args, PERCENT_MIN_MINUTES * 60, n)).fetchall()
        return [(uid, min(m / v, 1.0), m, v) for uid, m, v in rows]

    # --- Rollen-Inhaber -----------------------------------------------------
    def role_holder(self, guild_id: int, board: str) -> int | None:
        row = self.db.execute("SELECT user_id FROM role_holders WHERE guild_id = ? AND board = ?",
                              (guild_id, board)).fetchone()
        return row[0] if row else None

    def set_role_holder(self, guild_id: int, board: str, user_id: int | None):
        if user_id is None:
            self.db.execute("DELETE FROM role_holders WHERE guild_id = ? AND board = ?",
                            (guild_id, board))
        else:
            self.db.execute("INSERT OR REPLACE INTO role_holders VALUES (?, ?, ?)",
                            (guild_id, board, user_id))
        self.db.commit()


# --------------------------------------------------------------------------- #
# Hilfsfunktionen
# --------------------------------------------------------------------------- #
def in_counted_voice(state: discord.VoiceState | None, guild: discord.Guild) -> bool:
    if state is None or state.channel is None:
        return False
    return not (IGNORE_AFK and guild.afk_channel and state.channel.id == guild.afk_channel.id)


def mute_kind(state: discord.VoiceState | None, guild: discord.Guild) -> str | None:
    """DEAF, MUTE oder None. Taub schaltet in Discord das Mikro automatisch mit aus,
    deshalb zählt 'taub' immer als DEAF und nie zusätzlich als MUTE."""
    if not in_counted_voice(state, guild):
        return None
    if state.self_deaf:
        return DEAF
    if state.self_mute:
        return MUTE
    return None


def fmt_duration(seconds: float) -> str:
    m = int(seconds // 60)
    h, m = divmod(m, 60)
    return f"{h} h {m:02d} min" if h else f"{m} min"


def last_scheduled_post(now: dt.datetime) -> dt.datetime:
    """Letzter geplanter Post-Zeitpunkt, der <= now liegt."""
    monday = (now - dt.timedelta(days=now.weekday())).date()
    day = monday + dt.timedelta(days=POST_WEEKDAY)
    sched = dt.datetime.combine(day, dt.time(POST_HOUR, POST_MINUTE), tzinfo=TZ)
    if sched > now:
        sched -= dt.timedelta(days=7)
    return sched


MEDALS = ["🥇", "🥈", "🥉"]


def _rank(i: int) -> str:
    return MEDALS[i] if i < 3 else f"`{i + 1}.`"


def build_embed(store: Store, guild: discord.Guild, kind: str, all_time: bool,
                suffix: str) -> tuple[discord.Embed, int | None]:
    """Embed für eine Rangliste + User-ID des Ersten (oder None)."""
    b = BOARDS[kind]
    embed = discord.Embed(title=f"{b['icon']} {b['name']} – {suffix}", colour=b["colour"])
    embed.set_footer(text=guild.name)
    note = None

    if kind == PCT:
        if not all_time and store.period < store.pct_start:
            embed.description = "Startet mit der nächsten vollen Woche."
            return embed, None
        rows = store.percent(guild.id, all_time)
        lines = [f"{_rank(i)} <@{uid}> — **{q * 100:.0f} %** "
                 f"({fmt_duration(m)} von {fmt_duration(v)})"
                 for i, (uid, q, m, v) in enumerate(rows)]
        note = f"-# Nur wer mindestens {PERCENT_MIN_MINUTES} min im Voice war"
    else:
        rows = store.top(guild.id, kind, all_time)
        lines = [f"{_rank(i)} <@{uid}> — **{fmt_duration(sec)}**"
                 for i, (uid, sec) in enumerate(rows)]

    embed.description = fit_lines(lines, note) or b["empty"]
    return embed, (rows[0][0] if rows else None)


def fit_lines(lines: list[str], note: str | None = None) -> str:
    """Zeilen zusammenfügen, ohne das Discord-Limit zu sprengen. Passt nicht alles,
    endet die Liste mit '… und X weitere'."""
    if not lines:
        return ""
    tail = [note] if note else []
    out = []
    for i, line in enumerate(lines):
        rest = len(lines) - i
        reserve = len(f"… und {rest} weitere") + sum(len(t) + 1 for t in tail) + 2
        if len("\n".join(out + [line])) + reserve > EMBED_MAX:
            out.append(f"… und {rest} weitere")
            break
        out.append(line)
    return "\n".join(out + tail)


# --------------------------------------------------------------------------- #
# Bot
# --------------------------------------------------------------------------- #
class MuteBot(discord.Client):
    def __init__(self):
        # Bot spielt kein Audio ab -> Warnungen über fehlende Voice-Bibliotheken unterdrücken
        discord.VoiceClient.warn_nacl = discord.VoiceClient.warn_dave = False
        # default() enthält guilds + voice_states, keine privilegierten Intents nötig
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.store = Store(DB_PATH)
        # (guild, user, spur) -> (art, monotonic start); spur = "mute" oder "voice"
        self.active: dict[tuple[int, int, str], tuple[str, float]] = {}
        self.next_post_try = 0.0  # nach fehlgeschlagenem Wochenpost: frühestens dann erneut
        # Server des Leaderboard-Channels. Nur dort wird gemessen, da es nur einen
        # Wochenpost (und damit nur einen Wochenwechsel) gibt. Wird in on_ready gesetzt.
        self.home_guild_id: int | None = None
        self.synced = False  # Commands nur einmal syncen, nicht bei jedem Reconnect

    async def setup_hook(self):
        self.tick.start()

    # --- Session-Tracking ---------------------------------------------------
    def set_track(self, guild_id: int, user_id: int, track: str, kind: str | None):
        """Zustand einer Spur setzen; bei Wechsel wird die alte Session gespeichert."""
        key = (guild_id, user_id, track)
        current = self.active.get(key)
        if current and current[0] == kind:
            return
        if current:
            del self.active[key]
            self.store.add(guild_id, user_id, current[0], time.monotonic() - current[1])
        if kind:
            self.active[key] = (kind, time.monotonic())

    def update_member(self, guild: discord.Guild, member_id: int, state):
        self.set_track(guild.id, member_id, "mute", mute_kind(state, guild))
        self.set_track(guild.id, member_id, "voice",
                       VOICE if in_counted_voice(state, guild) else None)

    def flush(self):
        """Laufende Sessions in die DB schreiben, ohne sie zu beenden."""
        now = time.monotonic()
        for (gid, uid, track), (kind, start) in self.active.items():
            self.store.add(gid, uid, kind, now - start)
            self.active[(gid, uid, track)] = (kind, now)

    def reconcile(self):
        """Abgleich mit dem tatsächlichen Voice-Zustand (fängt verpasste Events ab)."""
        seen: dict[tuple[int, int, str], str] = {}
        guild = self.get_guild(self.home_guild_id) if self.home_guild_id else None
        if guild:
            for channel in [*guild.voice_channels, *guild.stage_channels]:
                for m in channel.members:
                    if m.bot or not in_counted_voice(m.voice, guild):
                        continue
                    seen[(guild.id, m.id, "voice")] = VOICE
                    if kind := mute_kind(m.voice, guild):
                        seen[(guild.id, m.id, "mute")] = kind
        for key in self.active.keys() - seen.keys():
            self.set_track(*key, None)
        for key, kind in seen.items():
            self.set_track(*key, kind)

    # --- Events -------------------------------------------------------------
    async def on_ready(self):
        # on_ready kommt nach jedem Reconnect erneut -> Einrichtung nur beim ersten Mal
        if self.home_guild_id is None:
            channel = self.get_channel(CHANNEL_ID) or await self.fetch_channel(CHANNEL_ID)
            self.home_guild_id = channel.guild.id
            others = [g.name for g in self.guilds if g.id != self.home_guild_id]
            if others:
                log.warning("Bot ist auch auf anderen Servern (%s). Gemessen wird nur auf '%s'.",
                            ", ".join(others), channel.guild.name)
        if not self.synced:
            guild = discord.Object(id=self.home_guild_id)  # Guild-Sync = sofort sichtbar
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            self.synced = True
        self.reconcile()
        log.info("Eingeloggt als %s, %d aktive Sessions", self.user, len(self.active))

    async def on_voice_state_update(self, member, before, after):
        if not member.bot and member.guild.id == self.home_guild_id:
            self.update_member(member.guild, member.id, after)

    # --- Minütlicher Takt: speichern + ggf. Wochenpost ----------------------
    @tasks.loop(minutes=1)
    async def tick(self):
        # Jeder Fehler wird abgefangen: Eine unbehandelte Exception würde die Schleife
        # für immer beenden und der Bot würde nichts mehr speichern.
        try:
            self.reconcile()
            self.flush()
        except Exception:
            log.exception("Fehler beim Speichern der Sessions")

        sched = last_scheduled_post(dt.datetime.now(TZ)).timestamp()
        last = self.store.get_meta("last_post")
        if last is None:  # erster Start: nicht sofort posten
            self.store.set_meta("last_post", sched)
        elif sched > last and time.monotonic() >= self.next_post_try:
            try:
                await self.post_weekly(sched)
            except discord.HTTPException as e:
                # Woche wird NICHT abgeschlossen -> Post wird später nachgeholt
                self.next_post_try = time.monotonic() + 600
                hint = {
                    50001: " – der Bot sieht den Channel nicht (Kanal ansehen fehlt oder falsche ID).",
                    50013: " – dem Bot fehlt im Channel 'Nachrichten senden' oder 'Links einbetten'.",
                }.get(getattr(e, "code", 0), "")
                log.error("Wochenpost fehlgeschlagen (%s)%s Neuer Versuch in 10 min.", e, hint)
                return
            except Exception:
                self.next_post_try = time.monotonic() + 600
                log.exception("Wochenpost fehlgeschlagen, neuer Versuch in 10 min")

    @tick.before_loop
    async def _wait_ready(self):
        await self.wait_until_ready()

    async def post_weekly(self, sched: float):
        """Ranglisten posten, Woche abschließen, Rollen verteilen. Wirft nur, solange
        noch nichts gesendet wurde. Danach gilt die Woche als erledigt, damit ein
        späterer Fehler nicht zu einem doppelten Post führt."""
        channel = self.get_channel(CHANNEL_ID) or await self.fetch_channel(CHANNEL_ID)
        guild = channel.guild
        embeds, winners, lines = [], {}, []
        for kind in (DEAF, MUTE, ALL, PCT):
            embed, winner = build_embed(self.store, guild, kind, False, "Woche")
            embeds.append(embed)
            winners[kind] = winner
            if winner:
                role = guild.get_role(ROLE_IDS[kind]) if ROLE_IDS[kind] else None
                label = role.mention if role else BOARDS[kind]["title_rank"]
                lines.append(f"{label}: <@{winner}>")
        content = ("👑 **Die Sieger der Woche**\n" + "\n".join(lines)) if lines else None
        # Alle Embeds einer Nachricht zusammen dürfen max. 6000 Zeichen haben ->
        # bei langen Listen auf mehrere Nachrichten verteilen.
        batches: list[list[discord.Embed]] = [[]]
        for embed in embeds:
            if batches[-1] and sum(map(len, batches[-1])) + len(embed) > MESSAGE_EMBEDS_MAX:
                batches.append([])
            batches[-1].append(embed)

        mentions = discord.AllowedMentions(users=True, roles=False)
        for i, batch in enumerate(batches):
            try:
                await channel.send(content=content if i == 0 else None, embeds=batch,
                                   allowed_mentions=mentions)
            except Exception:
                if i == 0:
                    raise  # noch nichts gesendet -> tick versucht es später erneut
                log.exception("Wochenpost unvollständig: Nachricht %d/%d fehlgeschlagen",
                              i + 1, len(batches))
                break

        self.store.next_period()
        self.store.set_meta("last_post", sched)
        try:
            await self.award_roles(guild, winners)
        except Exception:
            log.exception("Fehler beim Verteilen der Sieger-Rollen")

    async def award_roles(self, guild: discord.Guild, winners: dict[str, int | None]):
        """Rolle vom bisherigen Sieger nehmen und dem neuen geben. Fehler (fehlende
        Rechte, Mitglied hat den Server verlassen) werden geloggt, stoppen aber nichts."""
        for board, role_id in ROLE_IDS.items():
            if not role_id:
                continue
            role = guild.get_role(role_id)
            if role is None:
                log.warning("Rolle %s für %s nicht gefunden", role_id, board)
                continue
            prev, new = self.store.role_holder(guild.id, board), winners.get(board)
            if prev and prev != new:
                await self._change_role(guild, prev, role, add=False)
            if new:
                await self._change_role(guild, new, role, add=True)
            self.store.set_role_holder(guild.id, board, new)

    async def _change_role(self, guild, user_id: int, role, add: bool):
        try:
            member = guild.get_member(user_id) or await guild.fetch_member(user_id)
            if add:
                await member.add_roles(role, reason="Muteboard: Sieger der Woche")
            else:
                await member.remove_roles(role, reason="Muteboard: neue Woche")
        except discord.NotFound:
            log.info("Mitglied %s nicht mehr auf dem Server", user_id)
        except discord.Forbidden:
            log.warning("Keine Berechtigung für Rolle '%s' – hat der Bot 'Rollen verwalten' "
                        "und steht seine Rolle über '%s'?", role.name, role.name)
        except discord.HTTPException as e:
            log.warning("Rolle '%s' für %s konnte nicht geändert werden: %s", role.name, user_id, e)


bot = MuteBot()


@bot.tree.command(name="muteboard", description="Zeigt die Mute-Leaderboards")
@app_commands.describe(rangliste="Welche Rangliste", zeitraum="Diese Woche oder gesamt")
@app_commands.choices(
    rangliste=[
        app_commands.Choice(name="Full Mute (taub)", value=DEAF),
        app_commands.Choice(name="Nur Mute (Mikro aus)", value=MUTE),
        app_commands.Choice(name="Beides zusammen", value=ALL),
        app_commands.Choice(name="Stumm-Quote (% der Voice-Zeit)", value=PCT),
    ],
    zeitraum=[
        app_commands.Choice(name="Diese Woche", value="woche"),
        app_commands.Choice(name="Gesamt", value="gesamt"),
    ],
)
async def muteboard(interaction: discord.Interaction, rangliste: str = DEAF,
                    zeitraum: str = "woche"):
    if interaction.guild is None or interaction.guild.id != bot.home_guild_id:
        await interaction.response.send_message(
            "Das Muteboard läuft nur auf dem Server mit dem Leaderboard-Channel.", ephemeral=True)
        return
    bot.flush()  # damit laufende Sessions mitzählen
    all_time = zeitraum == "gesamt"
    embed, _ = build_embed(bot.store, interaction.guild, rangliste, all_time,
                           "gesamt" if all_time else "diese Woche")
    await interaction.response.send_message(embed=embed)


if __name__ == "__main__":
    bot.run(TOKEN, log_handler=None)
