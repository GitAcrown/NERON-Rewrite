import logging
from datetime import datetime,tzinfo
import pytz

import discord
from discord import Interaction, app_commands
from discord.ext import commands, tasks

from cogs.core.core import Core
from common import dataio

logger = logging.getLogger(f'NERON.{__name__.capitalize()}')

class Birthdays(commands.Cog):
    """Tracking des anniversaires des membres du serveur"""
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.data = dataio.get_cog_data(self)

        # Paramètres
        default_settings = {
            'NotificationChannelID': 0,
            'NotificationHour': '12',
            'BirthdayRoleID': 0,
        }
        self.data.register_keyvalue_table_for(discord.Guild, 'settings', default_values=default_settings)
        
        # Anniversaires
        birthdays = dataio.TableInitializer(
            table_name='birthdays',
            create_query="""CREATE TABLE IF NOT EXISTS birthdays (
                user_id INTEGER PRIMARY KEY,
                birthday TEXT,
                guilds_hidden TEXT DEFAULT ''
                )"""
        )
        self.data.register_tables_for('global', [birthdays])
        
        self.last_check : str = ''
    
        
    @commands.Cog.listener()
    async def on_ready(self):
        self.birthdays_loop.start()
        
        self.core : Core = self.bot.get_cog('Core') # type: ignore
        if not self.core:
            raise RuntimeError("Impossible de charger le module Core")
        
    def cog_unload(self):
        self.birthdays_loop.cancel()
        self.data.close_all()

        
    # Loop ------------------------------------------------------------------
    
    @tasks.loop(seconds=30)
    async def birthdays_loop(self):
        if self.last_check != datetime.now().strftime('%d/%m:%H'):
            self.last_check = datetime.now().strftime('%d/%m:%H')
            logger.info(f"Vérification des anniversaires, période : {self.last_check}")
            for guild in self.bot.guilds:
                birthdays = self.get_birthdays_today(guild)
                if not birthdays:
                        continue
                
                channel = self.get_birthday_channel(guild)
                if channel and isinstance(channel, (discord.TextChannel)):
                    if not self.is_notification_hour(guild):
                        continue
                    tz = self.get_timezone(guild)
                    zodiac = self.get_zodiac_sign(datetime.now(tz))
                    zodiac = f' · {zodiac[1]}' if zodiac else ''
                    txt = f"> ## Anniversaires aujourd'hui | {datetime.now().strftime('%d/%m')}{zodiac}\n"
                    for m in birthdays:
                        txt += f"> {m.mention}\n"
                    await channel.send(txt, silent=True)
                    
                if not self.is_role_attribution_hour(guild):
                    continue
                
                role = self.get_birthday_role(guild)
                if role:
                    for m in guild.members:
                        if m in birthdays:
                            continue
                        if role in m.roles:
                            await m.remove_roles(role, reason="Anniversaire terminé")
                            
                    for m in birthdays:
                        if role not in m.roles:
                            await m.add_roles(role, reason="Anniversaire")
        
    # Anniversaires ------------------------------------------------------------
    
    def get_user_birthday(self, user: discord.User | discord.Member) -> datetime | None:
        r = self.data.get('global').fetchone("""SELECT * FROM birthdays WHERE user_id = ?""", (user.id,))
        return datetime.strptime(r['birthday'], '%d/%m') if r else None
    
    def set_user_birthday(self, user: discord.User | discord.Member, birthday: str):
        self.data.get('global').execute("""INSERT OR REPLACE INTO birthdays (user_id, birthday) VALUES (?, ?)""", (user.id, birthday))
        
    def remove_user_birthday(self, user: discord.User | discord.Member):
        self.data.get('global').execute("""DELETE FROM birthdays WHERE user_id = ?""", (user.id,))
        
    # Préférences utilisateur -------------------------------------------------
    
    def get_user_settings(self, user: discord.User | discord.Member) -> dict | None:
        r = self.data.get('global').fetchone("""SELECT * FROM birthdays WHERE user_id = ?""", (user.id,))
        return {'birthday': r['birthday'], 'guilds_hidden': [int(g) for g in r['guilds_hidden'].split(',')] if r['guilds_hidden'] else []} if r else None
    
    def set_user_settings(self, user: discord.User | discord.Member, birthday: str, guilds_hidden: list[int]):
        self.data.get('global').execute(
            """INSERT OR REPLACE INTO birthdays (user_id, birthday, guilds_hidden) VALUES (?, ?, ?)""",
            (user.id, birthday, ','.join(map(str, guilds_hidden)))
        )
        
    def get_hidden_guilds(self, user: discord.User | discord.Member) -> list[int]:
        r = self.get_user_settings(user)
        return r['guilds_hidden'] if r else []
    
    def add_hidden_guild(self, user: discord.User | discord.Member, guild: discord.Guild) -> bool:
        r = self.get_user_settings(user)
        if r:
            guilds_hidden = r['guilds_hidden']
            if guild.id not in guilds_hidden:
                guilds_hidden.append(guild.id)
                self.set_user_settings(user, r['birthday'], guilds_hidden)
                return True
        return False
    
    def remove_hidden_guild(self, user: discord.User | discord.Member, guild: discord.Guild) -> bool:
        r = self.get_user_settings(user)
        if r:
            guilds_hidden = r['guilds_hidden']
            if guild.id in guilds_hidden:
                guilds_hidden.remove(guild.id)
                self.set_user_settings(user, r['birthday'], guilds_hidden)
                return True
        return False
    
    # Serveur -----------------------------------------------------------------
    
    def get_birthday_channel(self, guild: discord.Guild) -> discord.abc.GuildChannel | None:
        channel_id = self.data.get_keyvalue_table_value(guild, 'settings', 'NotificationChannelID', cast=int)
        return guild.get_channel(channel_id) if channel_id else None
    
    def get_timezone(self, guild: discord.Guild | None = None) -> tzinfo:
        if not guild:
            return pytz.timezone('Europe/Paris')
        tz = self.core.get_guild_global_setting(guild, 'Timezone')
        return pytz.timezone(tz) 
    
    def is_notification_hour(self, guild: discord.Guild) -> bool:
        """Vérifie si c'est l'heure d'envoyer les notifications d'anniversaire (heure définie dans les paramètres du serveur)"""
        hour = self.data.get_keyvalue_table_value(guild, 'settings', 'NotificationHour', cast=int)
        tz = self.get_timezone(guild)
        now = datetime.now(tz)
        return now.hour == int(hour)
    
    def is_role_attribution_hour(self, guild: discord.Guild) -> bool:
        """Vérifie si c'est l'heure d'attribuer le rôle d'anniversaire (minuit chaque jour selon le fuseau horaire du serveur)"""
        tz = self.get_timezone(guild)
        now = datetime.now(tz)
        return now.hour == 0
    
    def set_notification_hour(self, guild: discord.Guild, hour: int, tz: str):
        self.data.set_keyvalue_table_value(guild, 'settings', 'NotificationHour', hour)
        self.data.set_keyvalue_table_value(guild, 'settings', 'Timezone', tz)
    
    def get_birthday_role(self, guild: discord.Guild) -> discord.Role | None:
        role_id = self.data.get_keyvalue_table_value(guild, 'settings', 'BirthdayRoleID', cast=int)
        return guild.get_role(role_id) if role_id else None
    
    def get_birthdays(self, guild: discord.Guild) -> dict[discord.Member, datetime]:
        r = self.data.get('global').fetchall("""SELECT * FROM birthdays""")
        members = {m.id : m for m in guild.members}
        bdays = {}
        for u in r:
            if u['user_id'] in members:
                bdays[members[u['user_id']]] = datetime.strptime(u['birthday'], '%d/%m')
        return bdays
    
    # Global ------------------------------------------------------------------
    
    def get_birthdays_today(self, guild: discord.Guild) -> list[discord.Member]:
        return [m for m, d in self.get_birthdays(guild).items() if d.month == datetime.now().month and d.day == datetime.now().day and guild.id not in self.get_hidden_guilds(m)]
    
    # Utilitaires -------------------------------------------------------------
    
    def get_user_embed(self, user: discord.User | discord.Member) -> discord.Embed:
        date = self.get_user_birthday(user)
        if not date:
            return discord.Embed(title=f"Anniversaire de **{user.display_name}**", description="Aucune date d'anniversaire définie", color=0x2b2d31)
        if isinstance(user, discord.Member):
            tz = self.get_timezone(user.guild)
        else:
            tz = self.get_timezone()
        
        dt = date.replace(year=datetime.now().year).astimezone(tz)
        msg = f"**Date ·** {dt.strftime('%d/%m')}\n"

        # On calcule la date du prochain anniversaire
        today = datetime.now(tz)
        if today >= dt:
            next_date = dt.replace(year=today.year + 1)
        else:
            next_date = dt
        msg += f"**Prochain ·** <t:{int(next_date.timestamp())}:D>\n"
    
        astro = self.get_zodiac_sign(dt)
        if astro:
            msg += f"**Signe astro. ·** {' '.join(astro)}"
        
        embed = discord.Embed(title=f"Anniversaire de **{user.display_name}**", description=msg, color=0x2b2d31)
        embed.set_thumbnail(url=user.display_avatar.url)
        return embed
    
    def get_zodiac_sign(self, date: datetime) -> tuple[str, str] | None:
        zodiacs = [(120, 'Capricorne', '♑'), (218, 'Verseau', '♒'), (320, 'Poisson', '♓'), (420, 'Bélier', '♈'), (521, 'Taureau', '♉'),
           (621, 'Gémeaux', '♊'), (722, 'Cancer', '♋'), (823, 'Lion', '♌'), (923, 'Vierge', '♍'), (1023, 'Balance', '♎'),
           (1122, 'Scorpion', '♏'), (1222, 'Sagittaire', '♐'), (1231, 'Capricorne', '♑')]
        date_number = int(''.join((str(date.month), '%02d' % date.day)))
        for z in zodiacs:
            if date_number <= z[0]:
                return z[1], z[2]
    
    # COMMANDES ================================================================
    
    bday_group = app_commands.Group(name='bday', description="Anniversaires des membres du serveur")
    
    @bday_group.command(name='set')
    async def set_bday_command(self, interaction: Interaction, date: str):
        """Définir sa date d'anniversaire (sur tous les serveurs)

        :param date: Date au format JJ/MM
        """
        try:
            dt = datetime.strptime(date, '%d/%m')
        except ValueError:
            await interaction.response.send_message("**Erreur ** • La date est invalide ou n'est pas au bon format (JJ/MM)", ephemeral=True)
            return
        
        self.set_user_birthday(interaction.user, date)
        await interaction.response.send_message(f"**Date définie** • Vous avez indiqué être né.e le `{date}`", ephemeral=True)

    @bday_group.command(name='remove')
    async def remove_bday_command(self, interaction: Interaction):
        """Supprimer sa date d'anniversaire (de tous les serveurs)"""
        self.remove_user_birthday(interaction.user)
        await interaction.response.send_message("**Date supprimée** • Vous n'avez plus de date d'anniversaire définie", ephemeral=True)
        
    @bday_group.command(name='hide')
    @app_commands.rename(hide='cacher')
    async def hide_bday_command(self, interaction: Interaction, hide: bool):
        """Cacher/afficher sa date d'anniversaire sur ce serveur
        
        :param hide: True pour cacher, False pour afficher"""
        guild = interaction.guild
        if not isinstance(guild, discord.Guild):
            return await interaction.response.send_message("**Erreur** • Vous devez être sur le serveur où vous voulez cacher votre date d'anniversaire", ephemeral=True)
        
        if hide:
            if self.add_hidden_guild(interaction.user, guild):
                await interaction.response.send_message("**Date cachée** • Votre date d'anniversaire n'apparaîtra plus sur ce serveur", ephemeral=True)
            else:
                await interaction.response.send_message("**Erreur** • Votre date d'anniversaire est déjà cachée sur ce serveur ou n'est pas définie", ephemeral=True)
                
        else:
            if self.remove_hidden_guild(interaction.user, guild):
                await interaction.response.send_message("**Date affichée** • Votre date d'anniversaire apparaîtra à nouveau sur ce serveur", ephemeral=True)
            else:
                await interaction.response.send_message("**Erreur** • Votre date d'anniversaire n'est pas cachée sur ce serveur ou n'est pas définie", ephemeral=True)
    
    @bday_group.command(name='get')
    async def get_bday_command(self, interaction: Interaction, user: discord.Member | None = None):
        """Afficher la date d'anniversaire d'un membre
        
        :param user: Membre dont afficher la date d'anniversaire (laisser vide pour afficher la vôtre)"""
        member = user or interaction.user
        hidden = interaction.guild and interaction.guild.id in self.get_hidden_guilds(member)
        if hidden:
            return await interaction.response.send_message(f"**Non définie** • La date d'anniversaire de {member.mention} n'est pas définie sur ce serveur", ephemeral=True)
        embed = self.get_user_embed(member)
        await interaction.response.send_message(embed=embed)

    @bday_group.command(name='list')
    @app_commands.rename(limit='limite')
    async def list_bday_command(self, interaction: Interaction, limit: app_commands.Range[int, 5, 30] = 10):
        """Lister les anniversaires des membres de ce serveur
        
        :param limit: Nombre d'anniversaires à afficher"""
        if not isinstance(interaction.guild, discord.Guild):
            return await interaction.response.send_message("**Commande non disponible** • Cette commande n'est disponible que sur un serveur", ephemeral=True)
        
        birthdays = self.get_birthdays(interaction.guild)
        if not birthdays:
            return await interaction.response.send_message("**Aucun anniversaire** • Aucun membre n'a défini de date d'anniversaire")
        
        hidden = birthdays.copy()
        for m, d in birthdays.items():
            if interaction.guild.id in self.get_hidden_guilds(m):
                del hidden[m]
        if not hidden:
            return await interaction.response.send_message("**Aucun anniversaire** • Aucun membre n'a défini de date d'anniversaire sur ce serveur")
        birthdays = hidden
        
        today = datetime.now()
        for m, d in birthdays.items():
            d = d.replace(year=today.year)
            if d < today:
                d = d.replace(year=today.year + 1)
            birthdays[m] = d
        listebday = sorted(birthdays.items(), key=lambda x: x[1].timestamp())
        
        if not listebday:
            return await interaction.response.send_message("**Aucun anniversaire** • Aucun anniversaire n'est prévu dans les prochains jours")
        
        msg = ''
        year_changed = False
        for b in listebday[:limit]:
            user, date = b
            if date.year != today.year and not year_changed:
                msg += f"**`{date.year}` ――――――**\n"
                year_changed = True
            msg += f"{user.mention} · <t:{int(date.timestamp())}:D>\n"
        embed = discord.Embed(title="Prochains anniversaires", description=msg, color=0x2b2d31)
        embed.set_author(name=interaction.guild.name, icon_url=interaction.guild.icon.url if interaction.guild.icon else None)
        embed.set_footer(text=f"{limit if limit < len(listebday) else len(listebday)}/{len(listebday)} anniversaires affichés")
        await interaction.response.send_message(embed=embed)
        
    settings_group = app_commands.Group(name='config-bday', description="Configuration du module anniversaires", guild_only=True, default_permissions=discord.Permissions(manage_roles=True))
    
    @settings_group.command(name='channel')
    @app_commands.rename(channel='salon')
    async def channel_settings_command(self, interaction: Interaction, channel: discord.TextChannel | None = None):
        """Définir le salon où envoyer les notifications d'anniversaires
        
        :param channel: Salon où envoyer les notifications (laisser vide pour désactiver)"""
        if not isinstance(interaction.guild, discord.Guild):
            return await interaction.response.send_message("**Commande non disponible** • Cette commande n'est disponible que sur un serveur", ephemeral=True)
        
        if channel:
            self.data.set_keyvalue_table_value(interaction.guild, 'settings', 'NotificationChannelID', channel.id)
            await interaction.response.send_message(f"**Salon défini** • Les notifications d'anniversaires seront envoyées dans {channel.mention}")
        else:
            self.data.set_keyvalue_table_value(interaction.guild, 'settings', 'NotificationChannelID', 0)
            await interaction.response.send_message("**Salon supprimé** • Les notifications d'anniversaires ne seront plus envoyées")
            
    @settings_group.command(name='hour')
    @app_commands.rename(hour='heure')
    async def hour_settings_command(self, interaction: Interaction, hour: app_commands.Range[int, 0, 23]):
        """Définir l'heure où envoyer les notifications d'anniversaires
        
        :param hour: Heure à laquelle envoyer les notifications"""
        if not isinstance(interaction.guild, discord.Guild):
            return await interaction.response.send_message("**Commande non disponible** • Cette commande n'est disponible que sur un serveur", ephemeral=True)
        
        self.data.set_keyvalue_table_value(interaction.guild, 'settings', 'NotificationHour', hour)
        await interaction.response.send_message(f"**Heure définie** • Les notifications d'anniversaires seront envoyées à ***{hour}h***")
        
    @settings_group.command(name='role')
    @app_commands.rename(role='rôle')
    async def role_settings_command(self, interaction: Interaction, role: discord.Role | None = None):
        """Définir le rôle à attribuer aux membres le jour de leur anniversaire
        
        :param role: Rôle à attribuer (laisser vide pour désactiver)"""
        if not isinstance(interaction.guild, discord.Guild):
            return await interaction.response.send_message("**Commande non disponible** • Cette commande n'est disponible que sur un serveur", ephemeral=True)
        
        if role:
            self.data.set_keyvalue_table_value(interaction.guild, 'settings', 'BirthdayRoleID', role.id)
            await interaction.response.send_message(f"**Rôle défini** • Le rôle {role.mention} sera attribué aux membres le jour de leur anniversaire")
        else:
            self.data.set_keyvalue_table_value(interaction.guild, 'settings', 'BirthdayRoleID', 0)
            await interaction.response.send_message("**Rôle supprimé** • Aucun rôle ne sera attribué aux membres le jour de leur anniversaire")

async def setup(bot):
    await bot.add_cog(Birthdays(bot))
