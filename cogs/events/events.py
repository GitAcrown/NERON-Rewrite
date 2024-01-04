import logging
import re
from datetime import datetime, tzinfo

import discord
import pytz
from discord import Interaction, app_commands
from discord.ext import commands, tasks

from common import dataio
from common.utils import fuzzy, interface
from common.utils.pretty import DEFAULT_EMBED_COLOR, DEFAULT_ICONS_EMOJIS, shorten_text

logger = logging.getLogger(f'NERON.{__name__.split(".")[-1]}')

EVENTS_TYPES = {
    'on_member_join': {
        'name': 'Arrivée de membre',
        'format': {'member': 'Membre', 'guild': 'Serveur', 'time': "Heure"},
        'default': "**{member.mention}** a rejoint le serveur {guild} !"
    },
    'on_member_remove': {
        'name': 'Départ de membre',
        'format': {'member': 'Membre', 'guild': 'Serveur', 'time': "Heure"},
        'default': "**{member.mention}** a quitté le serveur {guild}."
    },
    'on_member_ban': {
        'name': 'Bannissement de membre',
        'format': {'member': 'Membre', 'guild': 'Serveur', 'time': "Heure"},
        'default': "**{member.mention}** a été banni du serveur {guild}."
    },
    'on_member_unban': {
        'name': 'Débannissement de membre',
        'format': {'member': 'Membre', 'guild': 'Serveur', 'time': "Heure"},
        'default': "**{member.mention}** a été débanni du serveur {guild}."
    }
}

SHARE_COOLDOWN_DELAY = 10 # secondes

class SubscribeToReminderView(discord.ui.View):
    """Ajoute un bouton permettant de s'inscrire à un rappel"""
    def __init__(self, cog: 'Events', reminder_data: dict, *, timeout: float | None = 300):
        super().__init__(timeout=timeout)
        self.__cog = cog
        self.reminder_data = reminder_data
        
        self.interaction : Interaction | None = None
        
    async def on_timeout(self):
        if self.interaction:
            await self.interaction.response.edit_message(view=None)
            
    @discord.ui.button(label="Être notifié", style=discord.ButtonStyle.primary, emoji=DEFAULT_ICONS_EMOJIS['ring'])
    async def subscribe(self, interaction: Interaction, button: discord.ui.Button):
        reminder_id = self.reminder_data['id']
        user = interaction.user
        if not isinstance(user, discord.Member) or not isinstance(interaction.guild, discord.Guild):
            return

        if self.__cog.add_reminder_user(interaction.guild, reminder_id, user.id):
            await interaction.response.send_message(f"**Inscription** • Vous avez été inscrit au rappel #{reminder_id}.", ephemeral=True)
        else:
            await interaction.response.send_message(f"**Inscription** • Vous êtes déjà inscrit au rappel #{reminder_id}.", ephemeral=True)
            
            
class Events(commands.Cog):
    """Suivi d'évenements sur le serveur et rappels"""
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.data = dataio.get_cog_data(self)
        
        default_settings = {
            'EnableReminderShare': 1,
            'SilentEventMentions': 0
        }
        self.data.register_keyvalue_table_for(discord.Guild, 'settings', default_values=default_settings)

        # Trackers actifs
        trackers = dataio.TableInitializer(
            table_name='trackers',
            create_query="""CREATE TABLE IF NOT EXISTS trackers (
                event_type TEXT PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                custom_message TEXT DEFAULT NULL
                )"""
        )
        # Rappels personnalisés
        reminders = dataio.TableInitializer(
            table_name='reminders',
            create_query="""CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                author_id INTEGER NOT NULL,
                userlist TEXT DEFAULT NULL
                )"""
        )
        self.data.register_tables_for(discord.Guild, [trackers, reminders])
        
        self.__reminders_cache : dict[int, list[dict]] = {}
        self.__reminders_share_cooldown : dict[int, int] = {}
        
    @commands.Cog.listener()
    async def on_ready(self):
        """Initialise les tâches"""
        self.reminders_loop.start()
        
        for guild in self.bot.guilds:
            self.update_reminders_cache(guild)
        
    def cog_unload(self):
        self.data.close_all()
        self.reminders_loop.cancel()
        
    # TACHES ============================================
    
    @tasks.loop(seconds=20)
    async def reminders_loop(self):
        """Vérifie les rappels et envoie les notifications"""
        cache = self.__reminders_cache.copy()
        for guild_id, reminders in cache.items():
            guild = self.bot.get_guild(guild_id)
            if not guild:
                continue
            for reminder in reminders:
                if int(reminder['timestamp']) <= datetime.now(tz=None).timestamp():
                    await self.handle_reminder(guild, reminder['id'])
        
    # EVENTS ============================================
    
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        tracker = self.get_tracker(member.guild, 'on_member_join')
        if not tracker:
            return
        
        channel = member.guild.get_channel(tracker['channel_id'])
        if not channel or not isinstance(channel, discord.TextChannel):
            return
        
        tz = self.get_timezone(member.guild)
        
        if tracker['custom_message']:
            message = tracker['custom_message'].format(member=member, guild=member.guild, time=datetime.now(tz=tz).strftime('%H:%M'))
        else:
            message = EVENTS_TYPES['on_member_join']['default'].format(member=member, guild=member.guild, time=datetime.now(tz=tz).strftime('%H:%M'))
        await channel.send(message, allowed_mentions=discord.AllowedMentions.none())
        
    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        tracker = self.get_tracker(member.guild, 'on_member_remove')
        if not tracker:
            return
        
        channel = member.guild.get_channel(tracker['channel_id'])
        if not channel or not isinstance(channel, discord.TextChannel):
            return
        
        tz = self.get_timezone(member.guild)
        
        if tracker['custom_message']:
            message = tracker['custom_message'].format(member=member, guild=member.guild, time=datetime.now(tz=tz).strftime('%H:%M'))
        else:
            message = EVENTS_TYPES['on_member_remove']['default'].format(member=member, guild=member.guild, time=datetime.now(tz=tz).strftime('%H:%M'))
        await channel.send(message, allowed_mentions=discord.AllowedMentions.none())
        
    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User):
        tracker = self.get_tracker(guild, 'on_member_ban')
        if not tracker:
            return
        
        channel = guild.get_channel(tracker['channel_id'])
        if not channel or not isinstance(channel, discord.TextChannel):
            return
        
        tz = self.get_timezone(guild)
        
        if tracker['custom_message']:
            message = tracker['custom_message'].format(member=user, guild=guild, time=datetime.now(tz=tz).strftime('%H:%M'))
        else:
            message = EVENTS_TYPES['on_member_ban']['default'].format(member=user, guild=guild, time=datetime.now(tz=tz).strftime('%H:%M'))
        await channel.send(message, allowed_mentions=discord.AllowedMentions.none())
        
    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User):
        tracker = self.get_tracker(guild, 'on_member_unban')
        if not tracker:
            return
        
        channel = guild.get_channel(tracker['channel_id'])
        if not channel or not isinstance(channel, discord.TextChannel):
            return
        
        tz = self.get_timezone(guild)
        
        if tracker['custom_message']:
            message = tracker['custom_message'].format(member=user, guild=guild, time=datetime.now(tz=tz).strftime('%H:%M'))
        else:
            message = EVENTS_TYPES['on_member_unban']['default'].format(member=user, guild=guild, time=datetime.now(tz=tz).strftime('%H:%M'))
        await channel.send(message, allowed_mentions=discord.AllowedMentions.none())
        
    # Partage des rappels ----------------------------
    
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Détecte les balises (&rX) de partage de rappel"""
        if not isinstance(message.guild, discord.Guild):
            return
        if message.author.bot:
            return
        if not self.data.get_keyvalue_table_value(message.guild, 'settings', 'EnableReminderShare', cast=bool):
            return
        if not message.channel.permissions_for(message.guild.me).send_messages:
            return
        if not message.content:
            return
        if message.guild.id in self.__reminders_share_cooldown:
            if int(datetime.now().timestamp()) - self.__reminders_share_cooldown[message.guild.id] < SHARE_COOLDOWN_DELAY:
                return
        
        # Récupération des balises (format : &rX avec X = id du rappel) - On prend que la première balise
        match = re.search(r'&r(\d+)', message.content.lower())
        if not match:
            return
        reminder_id = int(match.group(1))
        
        data = self.get_reminder(message.guild, reminder_id)
        if not data:
            return
        embed = self.get_reminder_embed(message.guild, data['id'], show_share=False)
        if not embed:
            return
        reminder_author = message.guild.get_member(data['author_id'])
        if not reminder_author:
            reminder_author = self.bot.user
        if reminder_author:
           embed.set_footer(text=f"Utilisez '/remindme subscribe' pour être notifié de ce rappel.", icon_url=reminder_author.display_avatar.url)
        await message.channel.send(embed=embed)
        self.__reminders_share_cooldown[message.guild.id] = int(datetime.now().timestamp())
    
    # Gestion des trackers ------------------------------
    
    def get_trackers(self, guild: discord.Guild):
        """Renvoie les trackers actifs sur le serveur"""
        r = self.data.get(guild).fetchall('SELECT * FROM trackers')
        return r
    
    def get_tracker(self, guild: discord.Guild, event_type: str) -> dict | None:
        """Renvoie le tracker d'un type donné"""
        r = self.data.get(guild).fetchone('SELECT * FROM trackers WHERE event_type = ?', (event_type,))
        return r if r else None
    
    def set_tracker(self, guild: discord.Guild, event_type: str, channel_id: int, custom_message: str = ''):
        """Définit le tracker d'un type donné"""
        if event_type not in EVENTS_TYPES:
            raise ValueError(f"Type d'évènement invalide : {event_type}")
        self.data.get(guild).execute("""INSERT OR REPLACE INTO trackers VALUES (?, ?, ?)""", (event_type, channel_id, custom_message))
    
    def remove_tracker(self, guild: discord.Guild, event_type: str):
        """Supprime le tracker d'un type donné"""
        self.data.get(guild).execute("""DELETE FROM trackers WHERE event_type = ?""", (event_type,))
        
    # Gestion des rappels -------------------------------
    
    def get_reminders(self, guild: discord.Guild):
        """Renvoie les rappels actifs sur le serveur"""
        r = self.data.get(guild).fetchall('SELECT * FROM reminders')
        return r
        
    def get_reminder(self, guild: discord.Guild, reminder_id: int) -> dict | None:
        """Renvoie le rappel d'un id donné"""
        r = self.data.get(guild).fetchone('SELECT * FROM reminders WHERE id = ?', (reminder_id,))
        return r if r else None
        
    def set_reminder(self, guild: discord.Guild, content: str, timestamp: int, channel: discord.TextChannel | discord.Thread, author: discord.Member, userlist: list[discord.Member] = []):
        """Définit un rappel"""
        users = ','.join([str(u.id) for u in userlist])
        self.data.get(guild).execute("""INSERT INTO reminders VALUES (NULL, ?, ?, ?, ?, ?)""", (content, timestamp, channel.id, author.id, users))
        self.update_reminders_cache(guild)
        
    def remove_reminder(self, guild: discord.Guild, reminder_id: int):
        """Supprime un rappel"""
        self.data.get(guild).execute("""DELETE FROM reminders WHERE id = ?""", (reminder_id,))
        self.update_reminders_cache(guild)
        
    def get_reminders_cache(self, guild: discord.Guild) -> list[dict]:
        """Renvoie le cache des rappels"""
        if guild.id not in self.__reminders_cache:
            self.__reminders_cache[guild.id] = []
        return self.__reminders_cache[guild.id]
        
    def update_reminders_cache(self, guild: discord.Guild):
        """Met à jour le cache des rappels"""
        self.__reminders_cache[guild.id] = self.get_reminders(guild)
        
    def get_reminder_users(self, guild: discord.Guild, reminder_id: int) -> list[discord.Member]:
        """Renvoie la liste des utilisateurs inscrits à un rappel"""
        reminder = self.get_reminder(guild, reminder_id)
        if not reminder:
            return []
        if not reminder['userlist']:
            return []
        l = [int(u) for u in reminder['userlist'].split(',') if u]
        members = {m.id: m for m in guild.members}
        return [members[m] for m in members if m in l]
    
    def add_reminder_user(self, guild: discord.Guild, reminder_id: int, user_id: int) -> bool:
        """Ajoute un utilisateur à un rappel
        
        :return: True si l'utilisateur a été ajouté, False sinon"""
        reminder = self.get_reminder(guild, reminder_id)
        if not reminder:
            return False
        if not reminder['userlist']:
            userlist = str(user_id)
        elif str(user_id) not in reminder['userlist'].split(','):
            userlist = f"{reminder['userlist']},{user_id}"
        else:
            return False
        self.data.get(guild).execute("""UPDATE reminders SET userlist = ? WHERE id = ?""", (userlist, reminder_id))
        self.update_reminders_cache(guild)
        return True
        
    def remove_reminder_user(self, guild: discord.Guild, reminder_id: int, user_id: int):
        """Supprime un utilisateur d'un rappel"""
        reminder = self.get_reminder(guild, reminder_id)
        if not reminder:
            return
        if not reminder['userlist']:
            return
        userlist = [int(u) for u in reminder['userlist'].split(',') if u]
        if user_id not in userlist:
            return
        userlist.remove(user_id)
        userlist = ','.join([str(u) for u in userlist])
        self.data.get(guild).execute("""UPDATE reminders SET userlist = ? WHERE id = ?""", (userlist, reminder_id))
        self.update_reminders_cache(guild)
    
    def get_reminder_channel(self, guild: discord.Guild, reminder_id: int) -> discord.TextChannel | discord.Thread | None:
        """Renvoie le salon de notification d'un rappel"""
        reminder = self.get_reminder(guild, reminder_id)
        if not reminder:
            return None
        channel = guild.get_channel(reminder['channel_id'])
        if not channel or not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return None
        return channel
        
    def get_reminder_embed(self, guild: discord.Guild, reminder_id: int, show_share: bool = True) -> discord.Embed | None:
        """Renvoie l'embed d'un rappel"""
        reminder = self.get_reminder(guild, reminder_id)
        if not reminder:
            return None
        timestamp = int(reminder['timestamp'])
        em = discord.Embed(title="Rappel", description=reminder['content'], color=DEFAULT_EMBED_COLOR)
        em.add_field(name="Date", value=f"<t:{timestamp}:R>")
        em.add_field(name="Notifié sur", value=f"<#{reminder['channel_id']}>")
        sharing = self.data.get_keyvalue_table_value(guild, 'settings', 'EnableReminderShare', cast=bool)
        if sharing and show_share:
            em.add_field(name="Partager", value=f"`&r{reminder_id}`")
        author = guild.get_member(reminder['author_id'])
        if not author:
            author = self.bot.user
        return em
    
    def small_reminder_embed(self, guild: discord.Guild, reminder_id: int) -> discord.Embed | None:
        """Renvoie un embed réduit pour un rappel"""
        reminder = self.get_reminder(guild, reminder_id)
        if not reminder:
            return None
        timestamp = int(reminder['timestamp'])
        em = discord.Embed(description=reminder['content'], color=DEFAULT_EMBED_COLOR, timestamp=datetime.fromtimestamp(timestamp))
        author = guild.get_member(reminder['author_id'])
        if not author:
            author = self.bot.user
        if author:
            em.set_footer(text=f"Rappel de {author.name}", icon_url=author.display_avatar.url)
        return em
    
    async def handle_reminder(self, guild: discord.Guild, reminder_id: int):
        """Envoie une notification pour un rappel puis le supprime"""
        reminder = self.get_reminder(guild, reminder_id)
        if not reminder:
            return
        channel = self.get_reminder_channel(guild, reminder_id)
        if not channel:
            return
        embed = self.small_reminder_embed(guild, reminder_id)
        if not embed:
            return
        users = self.get_reminder_users(guild, reminder_id)
        silent = self.data.get_keyvalue_table_value(guild, 'settings', 'SilentEventMentions', cast=bool)
        if not users:
            await channel.send(embed=embed)
        else:
            mentions = ' '.join([u.mention for u in users])
            await channel.send(content=mentions, embed=embed, silent=silent)
        self.remove_reminder(guild, reminder_id)
        
    # Utilitaires ---------------------------------------
    
    def get_timezone(self, guild: discord.Guild | None = None) -> tzinfo:
        if not guild:
            return pytz.timezone('Europe/Paris')
        core : Core = self.bot.get_cog('Core') # type: ignore
        if not core:
            return pytz.timezone('Europe/Paris')
        tz = core.get_guild_global_setting(guild, 'Timezone')
        return pytz.timezone(tz) 
    
    def extract_time_from_string(self, string: str, tz: tzinfo) -> datetime | None:
        """Extrait une date d'une chaîne de caractères
        
        :param string: Chaîne de caractères à analyser
        :param tz: Fuseau horaire à utiliser
        :return: Date extraite ou None
        """
        now = datetime.now()
        formats = [
            '%d/%m/%Y %H:%M',
            '%d/%m/%Y %H',
            '%d/%m/%Y',
            '%d/%m %H:%M',
            '%d/%m',
            '%d',
            '%H',
            '%H:%M'
        ]
        date = None
        for format in formats:
            try:
                date = datetime.strptime(string, format)
                break
            except ValueError:
                pass
        if date is None:
            date = now
        if date.year == 1900:
            if date.month < now.month:
                date = date.replace(year=now.year + 1)
            elif date.month == now.month and date.day < now.day:
                date = date.replace(year=now.year + 1)
            else:
                date = date.replace(year=now.year)
                
            if date.month == 1 and date.year == now.year:
                date = date.replace(month=now.month)
            if date.day == 1 and date.month == now.month and date.year == now.year:
                date = date.replace(day=now.day)
            
        return date.replace(tzinfo=tz)
        
    # COMMANDES =========================================
    
    # TRACKERS ------------------------------------------
    
    trackers_group = app_commands.Group(name='trackers', description="Gestion des trackers d'évènements", guild_only=True, default_permissions=discord.Permissions(manage_guild=True))
    
    @trackers_group.command(name='list')
    async def list_trackers_command(self, interaction: Interaction):
        """Affiche la liste des trackers actifs sur le serveur"""
        if not isinstance(interaction.guild, discord.Guild):
            return await interaction.response.send_message("**Indisponible** • Cette commande n'est pas disponible en message privé.", ephemeral=True)
        
        trackers = self.get_trackers(interaction.guild)
        if not trackers:
            return await interaction.response.send_message("**Aucun tracker** • Aucun tracker n'est actuellement actif sur ce serveur.", ephemeral=True)
        
        em = discord.Embed(title="Trackers actifs", color=DEFAULT_EMBED_COLOR)
        em.set_footer(text=f"Utilisez '/trackers set' pour ajouter un tracker et '/trackers remove' pour le supprimer.")
        
        for tracker in trackers:
            title = EVENTS_TYPES[tracker['event_type']]['name']
            content = f"**Salon** : <#{tracker['channel_id']}>"
            if tracker['custom_message']:
                content += f"\n**Message custom** : `{tracker['custom_message']}`"
            else:
                content += f"\n**Message** : `{EVENTS_TYPES[tracker['event_type']]['default']}`"
            em.add_field(name=title, value=content, inline=False)
        
        await interaction.response.send_message(embed=em)
        
    @trackers_group.command(name='set')
    @app_commands.rename(event_type='évènement', channel='salon', custom_message='message_custom')
    async def set_tracker_command(self, interaction: Interaction, event_type: str, channel: discord.TextChannel, *, custom_message: str = ''):
        """Définir un tracker d'évènement

        :param event_type: Type d'évènement à tracker
        :param channel: Salon dans lequel envoyer les messages
        :param custom_message: Message custom à envoyer (facultatif)
        """
        if not isinstance(interaction.guild, discord.Guild):
            return await interaction.response.send_message("**Indisponible** • Cette commande n'est pas disponible en message privé.", ephemeral=True)
        
        if event_type not in EVENTS_TYPES:
            return await interaction.response.send_message(f"**Type invalide** • Le type d'évènement `{event_type}` n'existe pas.", ephemeral=True)
        
        if not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message(f"**Salon invalide** • Le salon `{channel}` n'est pas un salon écrit valide.", ephemeral=True)
        
        if not channel.permissions_for(interaction.guild.me).send_messages:
            return await interaction.response.send_message(f"**Permissions insuffisantes** • Je n'ai pas la permission d'envoyer des messages dans le salon {channel.mention}.", ephemeral=True)
        
        self.set_tracker(interaction.guild, event_type, channel.id, custom_message)
        if custom_message:
            return await interaction.response.send_message(f"**Tracker défini** • Le tracker d'évènement `{event_type}` a été défini dans le salon {channel.mention} avec le message custom `{custom_message}`.", ephemeral=True)
        await interaction.response.send_message(f"**Tracker défini** • Le tracker d'évènement `{event_type}` a été défini dans le salon {channel.mention}.", ephemeral=True)
        
    @trackers_group.command(name='remove')
    @app_commands.rename(event_type='évènement')
    async def remove_tracker_command(self, interaction: Interaction, event_type: str):
        """Supprimer un tracker d'évènement

        :param event_type: Type d'évènement à arrêter de tracker
        """
        if not isinstance(interaction.guild, discord.Guild):
            return await interaction.response.send_message("**Indisponible** • Cette commande n'est pas disponible en message privé.", ephemeral=True)
        
        if event_type not in EVENTS_TYPES:
            return await interaction.response.send_message(f"**Type invalide** • Le type d'évènement `{event_type}` n'existe pas.", ephemeral=True)
        
        if not self.get_tracker(interaction.guild, event_type):
            return await interaction.response.send_message(f"**Tracker introuvable** • Le tracker d'évènement `{event_type}` n'est pas actif sur ce serveur.", ephemeral=True)
        
        self.remove_tracker(interaction.guild, event_type)
        await interaction.response.send_message(f"**Tracker supprimé** • Le tracker d'évènement `{event_type}` a été supprimé.", ephemeral=True)
        
    @set_tracker_command.autocomplete('event_type')
    @remove_tracker_command.autocomplete('event_type')
    async def autocomplete_event_type(self, interaction: Interaction, current: str):
        r = fuzzy.finder(current, [(k, v['name']) for k, v in EVENTS_TYPES.items()])
        return [app_commands.Choice(name=n, value=k) for k, n in r]
    
    @set_tracker_command.autocomplete('custom_message')
    async def autocomplete_custom_message(self, interaction: Interaction, current: str):
        current_type = interaction.namespace.évènement
        if current_type not in EVENTS_TYPES:
            return []
        elements = EVENTS_TYPES[current_type]['format']
        return [app_commands.Choice(name=f'{{{k}}}', value=f'{{{k}}}') for k, _ in elements.items()]
    
    # RAPPELS -------------------------------------------
    
    reminders_group = app_commands.Group(name='remindme', description="Gestion des rappels", guild_only=True)
    
    @reminders_group.command(name='list')
    @app_commands.rename(all_reminders='tous')
    async def list_reminders_command(self, interaction: Interaction, all_reminders: bool = False):
        """Affiche la liste des rappels auxquels vous êtes inscrit
        
        :param all_reminders: Afficher tous les rappels du serveur (facultatif)"""
        if not isinstance(interaction.guild, discord.Guild) or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("**Indisponible** • Cette commande n'est pas disponible en message privé.", ephemeral=True)
        
        await interaction.response.defer(ephemeral=True)
        reminders = self.get_reminders(interaction.guild)
        if not reminders:
            return await interaction.followup.send("**Aucun rappel** • Aucun rappel n'est actuellement actif sur ce serveur.", ephemeral=True)
        
        if not all_reminders:
            reminders = [r for r in reminders if interaction.user.id in [int(u) for u in r['userlist'].split(',') if u]]
        if not reminders:
            return await interaction.followup.send("**Aucun rappel** • Vous n'êtes inscrit à aucun rappel sur ce serveur.", ephemeral=True)
        
        embeds = []
        emtitle = "Tous les rappels programmés" if all_reminders else "Vos rappels programmés"
        current_embed = discord.Embed(title=emtitle, color=DEFAULT_EMBED_COLOR)
        for r in reminders:
            if len(current_embed.fields) >= 20:
                current_embed.set_footer(text=f"Page {len(embeds)+1}")
                embeds.append(current_embed)
                current_embed = discord.Embed(title=f"Rappels actifs {'(tous)' if all_reminders else ''}", color=DEFAULT_EMBED_COLOR)
            title = f"• Rappel `{r['id']}`"
            nb_inscrits = len([int(u) for u in r['userlist'].split(',') if u])
            content = f"**Contenu** : {r['content']}\n**Date** : <t:{int(r['timestamp'])}:R>\n**Sur** : <#{r['channel_id']}>\n**Inscrits** : {nb_inscrits}"
            current_embed.add_field(name=title, value=content, inline=False)
        if embeds:
            current_embed.set_footer(text=f"Page {len(embeds)+1}")
        else:
            current_embed.set_footer(text="Utilisez '&rX' pour partager un rappel.")
        embeds.append(current_embed)
        
        if len(embeds) == 1:
            await interaction.followup.send(embed=embeds[0])
        else:
            view = interface.EmbedPaginatorMenu(embeds=embeds, timeout=30, users=[interaction.user])
            await view.start(interaction)
            
    @reminders_group.command(name='create')
    @app_commands.rename(content='contenu', time='date', self_sub='sinscrire')
    async def create_reminder_command(self, interaction: Interaction, content: str, time: str, *, self_sub: bool = True):
        """Créer un rappel

        :param content: Contenu du rappel
        :param time: Date du rappel au format JJ/MM HH:MM
        :param self_sub: Vous inscrire au rappel (activé par défaut)
        """
        if not isinstance(interaction.guild, discord.Guild) or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("**Indisponible** • Cette commande n'est pas disponible en message privé.", ephemeral=True)
        
        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return await interaction.response.send_message(f"**Salon invalide** • Le salon {channel} n'est pas un salon écrit valide.", ephemeral=True)
        
        if not channel.permissions_for(interaction.guild.me).send_messages:
            return await interaction.response.send_message(f"**Permissions insuffisantes** • Je n'ai pas la permission d'envoyer des messages dans le salon {channel.mention}.", ephemeral=True)
        
        if len(content) > 2000:
            return await interaction.response.send_message(f"**Contenu invalide** • Le contenu du rappel ne doit pas dépasser 2000 caractères.", ephemeral=True)
        
        tz = self.get_timezone(interaction.guild)
        date = self.extract_time_from_string(time, tz)
        if not date:
            return await interaction.response.send_message(f"**Date invalide** • La date `{time}` n'est pas valide.", ephemeral=True)
        if date < datetime.now(tz=tz):
            return await interaction.response.send_message(f"**Date invalide** • La date `{time}` est déjà passée.", ephemeral=True)
        
        # On enregistre le temps en naif
        date = date.replace(tzinfo=None)
        self.set_reminder(interaction.guild, content, int(date.timestamp()), channel, interaction.user, [interaction.user] if self_sub else [])
        
        data = self.get_reminders_cache(interaction.guild)[-1]
        view = SubscribeToReminderView(self, data)
        embed = self.get_reminder_embed(interaction.guild, data['id'])
        if not embed:
            return await interaction.response.send_message(f"**Rappel créé** • Le rappel a été créé dans le salon {channel.mention}.")
        
        await interaction.response.defer()
        embed.set_footer(text="Cliquez sur le bouton ci-dessous pour être notifié du rappel.", icon_url=interaction.user.display_avatar.url)
        await interaction.followup.send(f"**Rappel créé** • Ce rappel a été créé pour le salon {channel.mention} :", view=view, embed=embed)
        await view.wait()
        embed.set_footer(text=f"Utilisez '/remindme subscribe' pour être notifié du rappel.", icon_url=interaction.user.display_avatar.url)
        await interaction.edit_original_response(embed=embed, view=None)
        
    @create_reminder_command.autocomplete('time')
    async def autocomplete_time(self, interaction: Interaction, current: str):
        tz = self.get_timezone(interaction.guild)
        date = self.extract_time_from_string(current, tz)
        if not date:
            return []
        return [app_commands.Choice(name=date.strftime('%d/%m/%Y %H:%M'), value=date.strftime('%d/%m/%Y %H:%M'))]
        
    @reminders_group.command(name='delete')
    @app_commands.rename(reminder_id='rappel')
    async def delete_reminder_command(self, interaction: Interaction, reminder_id: int):
        """Supprimer un rappel

        :param reminder_id: Identifiant du rappel
        """
        if not isinstance(interaction.guild, discord.Guild) or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("**Indisponible** • Cette commande n'est pas disponible en message privé.", ephemeral=True)
        
        reminder = self.get_reminder(interaction.guild, reminder_id)
        if not reminder:
            return await interaction.response.send_message(f"**Rappel introuvable** • Ce rappel n'existe pas.", ephemeral=True)
        
        if interaction.user.id != reminder['author_id'] or not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message(f"**Permissions insuffisantes** • Vous n'êtes pas l'auteur du rappel #{reminder_id}.", ephemeral=True)
        
        embed = self.get_reminder_embed(interaction.guild, reminder_id)
        if not embed:
            return await interaction.response.send_message(f"**Rappel introuvable** • Ce rappel n'existe pas.", ephemeral=True)
        
        await interaction.response.defer(ephemeral=True)
        if not await interface.ask_confirm(interaction, f"**Suppression** • Êtes-vous sûr de vouloir supprimer ce rappel ?", embeds=[embed]):
            return await interaction.followup.send(f"**Suppression annulée** • Le rappel `#{reminder_id}` n'a pas été supprimé.", ephemeral=True)
        
        self.remove_reminder(interaction.guild, reminder_id)
        await interaction.followup.send(f"**Rappel supprimé** • Le rappel `#{reminder_id}` a été supprimé.", ephemeral=True)
    
    @reminders_group.command(name='subscribe')
    @app_commands.rename(reminder_id='rappel')
    async def subscribe_reminder_command(self, interaction: Interaction, reminder_id: int):
        """S'inscrire à un rappel déjà créé

        :param reminder_id: Identifiant du rappel
        """
        if not isinstance(interaction.guild, discord.Guild) or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("**Indisponible** • Cette commande n'est pas disponible en message privé.", ephemeral=True)
        
        reminder = self.get_reminder(interaction.guild, reminder_id)
        if not reminder:
            return await interaction.response.send_message(f"**Rappel introuvable** • Ce rappel n'existe pas.", ephemeral=True)
        
        if interaction.user.id in [int(u) for u in reminder['userlist'].split(',') if u]:
            return await interaction.response.send_message(f"**Déjà inscrit** • Vous êtes déjà inscrit au rappel.", ephemeral=True)
        
        embed = self.get_reminder_embed(interaction.guild, reminder_id)
        if not embed:
            return await interaction.response.send_message(f"**Rappel introuvable** • Ce rappel n'existe pas.", ephemeral=True)
        
        await interaction.response.defer(ephemeral=True)
        if not await interface.ask_confirm(interaction, f"**Inscription** • Êtes-vous sûr de vouloir vous inscrire à ce rappel ?", embeds=[embed]):
            return await interaction.followup.send(f"**Inscription annulée** • Vous n'êtes pas inscrit au rappel.", ephemeral=True)
        
        self.add_reminder_user(interaction.guild, reminder_id, interaction.user.id)
        await interaction.followup.send(f"**Inscription** • Vous avez été inscrit au rappel `#{reminder_id}`.", ephemeral=True)
        
    @reminders_group.command(name='unsubscribe')
    @app_commands.rename(reminder_id='rappel')
    async def unsubscribe_reminder_command(self, interaction: Interaction, reminder_id: int):
        """Se désinscrire d'un rappel créé

        :param reminder_id: Identifiant du rappel
        """
        if not isinstance(interaction.guild, discord.Guild) or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("**Indisponible** • Cette commande n'est pas disponible en message privé.", ephemeral=True)
        
        reminder = self.get_reminder(interaction.guild, reminder_id)
        if not reminder:
            return await interaction.response.send_message(f"**Rappel introuvable** • Ce rappel n'existe pas.", ephemeral=True)
        
        if interaction.user.id not in [int(u) for u in reminder['userlist'].split(',') if u]:
            return await interaction.response.send_message(f"**Non inscrit** • Vous n'êtes pas inscrit au rappel.", ephemeral=True)
        
        self.remove_reminder_user(interaction.guild, reminder_id, interaction.user.id)
        await interaction.response.send_message(f"**Désinscription** • Vous avez été désinscrit du rappel `#{reminder_id}`.", ephemeral=True)
        
    @delete_reminder_command.autocomplete('reminder_id')
    @subscribe_reminder_command.autocomplete('reminder_id')
    @unsubscribe_reminder_command.autocomplete('reminder_id')
    async def autocomplete_reminder_id(self, interaction: Interaction, current: str):
        if not isinstance(interaction.guild, discord.Guild):
            return []
        reminders = self.get_reminders(interaction.guild)
        if not reminders:
            return []
        return [app_commands.Choice(name=f"#{r['id']} • {shorten_text(r['content'], 30)}", value=r['id']) for r in reminders][:10]
    
    config_reminders_group = app_commands.Group(name='config-remindme', description="Configuration du système de rappels", guild_only=True, default_permissions=discord.Permissions(manage_messages=True))
    
    @config_reminders_group.command(name='autoshare')
    @app_commands.rename(autoshare='activer')
    async def autoshare_reminders_command(self, interaction: Interaction, autoshare: bool):
        """Activer/désactiver le partage des rappels lorsque l'identifiant est mentionné sur un salon
        
        :param autoshare: Activer/désactiver le partage automatique"""
        if not isinstance(interaction.guild, discord.Guild):
            return await interaction.response.send_message("**Indisponible** • Cette commande n'est pas disponible en message privé.", ephemeral=True)
        
        self.data.set_keyvalue_table_value(interaction.guild, 'settings', 'EnableReminderShare', int(autoshare))
        await interaction.response.send_message(f"**Partage automatique** • Le partage automatique des rappels a été {'activé' if autoshare else 'désactivé'}.", ephemeral=True)
    
    @config_reminders_group.command(name='silent')
    @app_commands.rename(silent='silencieux')
    async def silent_reminders_command(self, interaction: Interaction, silent: bool):
        """Activer/désactiver le mode silencieux pour les mentions lors des rappels
        
        :param silent: Activer/désactiver le mode silencieux"""
        if not isinstance(interaction.guild, discord.Guild):
            return await interaction.response.send_message("**Indisponible** • Cette commande n'est pas disponible en message privé.", ephemeral=True)
        
        self.data.set_keyvalue_table_value(interaction.guild, 'settings', 'SilentEventMentions', int(silent))
        await interaction.response.send_message(f"**Mode silencieux** • Le mode silencieux a été {'activé' if silent else 'désactivé'}.", ephemeral=True)
        
async def setup(bot):
    await bot.add_cog(Events(bot))
