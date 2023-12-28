import asyncio
import logging
from datetime import datetime, tzinfo
from typing import Any

import aiohttp
import discord
from discord import Interaction, app_commands
from discord.ext import commands, tasks
import pytz

from common import dataio
from common.utils.pretty import DEFAULT_EMBED_COLOR
from common.utils import fuzzy

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

class Events(commands.Cog):
    """Suivi d'évenements sur le serveur"""
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.data = dataio.get_cog_data(self)

        # Trackers actifs
        trackers = dataio.TableInitializer(
            table_name='trackers',
            create_query="""CREATE TABLE IF NOT EXISTS trackers (
                event_type TEXT PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                custom_message TEXT DEFAULT NULL
                )"""
        )
        self.data.register_tables_for(discord.Guild, [trackers])
        
    def cog_unload(self):
        self.data.close_all()
    
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
        
    # Utilitaires ---------------------------------------
    
    def get_timezone(self, guild: discord.Guild | None = None) -> tzinfo:
        if not guild:
            return pytz.timezone('Europe/Paris')
        core : Core = self.bot.get_cog('Core') # type: ignore
        if not core:
            return pytz.timezone('Europe/Paris')
        tz = core.get_guild_global_setting(guild, 'Timezone')
        return pytz.timezone(tz) 
        
    # COMMANDES =========================================
    
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
        
async def setup(bot):
    await bot.add_cog(Events(bot))
