import asyncio
import logging
from datetime import datetime
from typing import Any

import aiohttp
import discord
from discord import Interaction, app_commands
from discord.ext import commands, tasks

from common import dataio

logger = logging.getLogger(f'NERON.{__name__.capitalize()}')

HISTORY_EXPIRATION = 60 * 60 * 72 # 3 jours

class MsgBoard(commands.Cog):
    """Compilation des meilleurs messages du serveur."""
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.data = dataio.get_cog_data(self)

        # Paramètres
        default_settings = {
            'Enabled': 0,
            'Threshold': 3,
            'Emoji': '⭐',
            'NotifyHalfThreshold': 0,
            'Webhook_URL': '',
            'MaxMessageAge': 60 * 60 * 24 # 24 heures par défaut
        }
        self.data.register_keyvalue_table_for(discord.Guild, 'settings', default_values=default_settings)
        
        # Historique des messages repostés
        board_history = dataio.TableInitializer(
            table_name='board_history',
            create_query="""CREATE TABLE IF NOT EXISTS board_history (
                message_id INTEGER PRIMARY KEY,
                reposted BOOLEAN DEFAULT 0 CHECK (reposted IN (0, 1)),
                notification_id INTEGER DEFAULT 0,
                timestamp INTEGER
                )"""
        )
        self.data.register_tables_for(discord.Guild, [board_history])
        
        self.clean_history.start()
        
    def cog_unload(self):
        self.clean_history.cancel()
        self.data.close_all()
        
    # Nettoyage ---------------------------------------------------------------
    
    @tasks.loop(hours=12)
    async def clean_history(self):
        """Nettoie l'historique des messages."""
        for guild in self.data.get_all():
            if not isinstance(guild, discord.Guild):
                continue
            
            self.data.get(guild).execute("DELETE FROM board_history WHERE timestamp < ?", (int(datetime.utcnow().timestamp()) - HISTORY_EXPIRATION,))
            
    # Webhook -----------------------------------------------------------------
    
    async def repost_message(self, message: discord.Message):
        """Reposte un message sur le salon de compilation."""
        if not isinstance(message.guild, discord.Guild) or not isinstance(message.author, discord.Member):
            raise TypeError("Le message doit provenir d'un membre d'un serveur.")
        
        webhook_url = self.data.get_keyvalue_table_value(message.guild, 'settings', 'Webhook_URL')
        if not webhook_url:
            return
        
        jump_to_button = discord.ui.Button(label='Aller au message', url=message.jump_url)
        jump_view = discord.ui.View()
        jump_view.add_item(jump_to_button)
        
        reply = message.reference.resolved if message.reference else None
        reply_content = ''
        if reply and isinstance(reply, discord.Message):
            reply_msg = await message.channel.fetch_message(reply.id)
            reply_content = f"> **{reply_msg.author.name}** · <t:{int(reply_msg.created_at.timestamp())}>"
            if reply_msg.content:
                reply_content += f"\n> {reply_msg.content}"
            if reply_msg.attachments:
                attachments_links = ' '.join([attachment.url for attachment in reply_msg.attachments])
                reply_content += f"\n> {attachments_links}"
                
        files, extra = [], []
        if message.attachments:
            files = [await attachment.to_file() for attachment in message.attachments if attachment.size < 8388608]
            extra = [attachment.url for attachment in message.attachments if attachment.size >= 8388608]
            
        content = f"{reply_content}\n{message.content}" if message.content else reply_content
        if extra:
            content += '\n' + ' '.join(extra)
        
        async with aiohttp.ClientSession() as session:
            webhook = discord.Webhook.from_url(webhook_url, session=session, client=self.bot)
            await webhook.send(
                content=content,
                username=message.author.name,
                avatar_url=message.author.display_avatar.url,
                embeds=message.embeds,
                files=files,
                silent=True,
                view=jump_view
            )
            
    # Notifications ------------------------------------------------------------
    
    def get_message_history(self, message: discord.Message) -> dict[str, Any]:
        """Renvoie l'historique d'un message."""
        if not isinstance(message.guild, discord.Guild):
            raise ValueError("Le message doit être sur un serveur.")
        
        r = self.data.get(message.guild).fetchone("SELECT * FROM board_history WHERE message_id = ?", (message.id,))
        return dict(r) if r else dict()
    
    def set_message_history(self, message: discord.Message, *, reposted: bool = False, notification_id: int = 0):
        """Modifie l'historique d'un message."""
        if not isinstance(message.guild, discord.Guild):
            raise ValueError("Le message doit être sur un serveur.")
        
        # On vérifie que le message est bien dans l'historique
        if not self.get_message_history(message):
            self.data.get(message.guild).execute("INSERT OR IGNORE INTO board_history (message_id, timestamp) VALUES (?, ?)", (message.id, int(message.created_at.timestamp())))
            
        if reposted:
            self.data.get(message.guild).execute("UPDATE board_history SET reposted = 1 WHERE message_id = ?", (message.id,))
        if notification_id:
            self.data.get(message.guild).execute("UPDATE board_history SET notification_id = ? WHERE message_id = ?", (notification_id, message.id))
    
    async def send_half_threshold_notification(self, message: discord.Message, current_votes: int):
        """Envoie une notification lorsque le seuil de notification est atteint."""
        if not isinstance(message.guild, discord.Guild) or not isinstance(message.author, discord.Member):
            raise TypeError("Le message doit provenir d'un membre d'un serveur.")
        
        channel = message.channel
        if not channel.permissions_for(message.guild.me).manage_messages:
            return
        
        threshold = self.data.get_keyvalue_table_value(message.guild, 'settings', 'Threshold')
        emoji = self.data.get_keyvalue_table_value(message.guild, 'settings', 'Emoji')
        
        text = f"`{emoji}` **Message populaire** • Ce message possède {threshold}{emoji} et sera reposté s'il atteint {current_votes}{emoji} !"
        notif_msg = await message.reply(text, mention_author=False)
        self.set_message_history(message, notification_id=notif_msg.id)
        
        await asyncio.sleep(120)
        try:
            await notif_msg.delete()
        except discord.NotFound:
            pass
        
    async def send_threshold_notification(self, message: discord.Message):
        """Envoie une notification lorsque le seuil de repost est atteint."""
        if not isinstance(message.guild, discord.Guild) or not isinstance(message.author, discord.Member):
            raise TypeError("Le message doit provenir d'un membre d'un serveur.")
        
        channel = message.channel
        if not channel.permissions_for(message.guild.me).manage_messages:
            return
        
        emoji = self.data.get_keyvalue_table_value(message.guild, 'settings', 'Emoji')
        
        text = f"## `{emoji}` **Message populaire** • Ce message a été reposté sur le salon de compilation !"
        await message.reply(text, delete_after=60, mention_author=False)
        self.set_message_history(message, reposted=True)
        
    # Events ------------------------------------------------------------------	
    
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        channel = self.bot.get_channel(payload.channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        if not channel.guild:
            return
        if not channel.permissions_for(channel.guild.me).read_message_history:
            return
        guild = channel.guild
        if not self.data.get_keyvalue_table_value(guild, 'settings', 'Webhook_URL'):
            return
        
        
        reaction_emoji = payload.emoji.name
        if reaction_emoji != self.data.get_keyvalue_table_value(guild, 'settings', 'Emoji'):
            return
        message = await channel.fetch_message(payload.message_id)
        if not message:
            return  
        
        maxage = self.data.get_keyvalue_table_value(guild, 'settings', 'MaxMessageAge', cast=int)
        if message.created_at.timestamp() < (datetime.utcnow().timestamp() - maxage):
            return
        
        votes_count = [reaction.count for reaction in message.reactions if str(reaction.emoji) == reaction_emoji]
        if not votes_count:
            return
        votes_count = votes_count[0]
        
        threshold = self.data.get_keyvalue_table_value(guild, 'settings', 'Threshold', cast=int)
        
        notif = self.data.get_keyvalue_table_value(guild, 'settings', 'NotifyHalfThreshold', cast=bool)
        if notif:
            notif_threshold = threshold // 2 + 1
            if votes_count == notif_threshold and not self.get_message_history(message).get('notification_id'):
                await self.send_half_threshold_notification(message, votes_count)
        
        if votes_count == threshold and not self.get_message_history(message).get('reposted'):
            notif_message_id = self.get_message_history(message).get('notification_id')
            if notif_message_id:
                try:
                    notif_message = await channel.fetch_message(notif_message_id)
                    await notif_message.delete()
                except discord.NotFound:
                    pass
            
            await self.send_threshold_notification(message)
            await self.repost_message(message)
            
    # Configuration ============================================================
    
    config_group = app_commands.Group(name='msgboard',
                                      description="Configuration du salon de compilation des meilleurs messages",
                                      guild_only=True,
                                      default_permissions=discord.Permissions(manage_messages=True))
        
    @config_group.command(name='enable')
    @app_commands.rename(enable='activer')
    async def enable_msgboard(self, interaction: Interaction, enable: bool):
        """Active ou désactive le salon de compilation des meilleurs messages
        
        :param enable: True pour activer, False pour désactiver"""
        if not isinstance(interaction.guild, discord.Guild):
            raise TypeError("La commande doit être utilisée sur un serveur.")
        
        self.data.set_keyvalue_table_value(interaction.guild, 'settings', 'Enabled', int(enable))
        await interaction.response.send_message(f"**Succès** • Le salon de compilation des meilleurs messages a été {'' if enable else 'dés'}activé.\nUtilisez `/msgboard channel` pour configurer le salon.", ephemeral=True)
        
    @config_group.command(name='channel')
    @app_commands.rename(channel='salon')
    async def set_msgboard_channel(self, interaction: Interaction, channel: discord.TextChannel):
        """Définit le salon de compilation des meilleurs messages
        
        :param channel: Le salon"""
        if not isinstance(interaction.guild, discord.Guild) or not isinstance(interaction.channel, (discord.TextChannel | discord.Thread)):
            raise ValueError("L'interaction doit être sur un serveur.")
        
        if not self.data.get_keyvalue_table_value(interaction.guild, 'settings', 'Enabled', cast=bool):
            return await interaction.response.send_message("**Erreur** • Activez d'abord le message board avec `/msgboard enable`.", ephemeral=True)
        
        await interaction.response.defer(ephemeral=True)
        current_webhook_url = self.data.get_keyvalue_table_value(interaction.guild, 'settings', 'Webhook_URL')
        if not channel:
            if not current_webhook_url:
                await interaction.followup.send("**Salon du message board** • Aucun salon n'est défini pour le message board.", ephemeral=True)
            else:
                webhook = discord.Webhook.from_url(current_webhook_url, client=self.bot)
                webhook = await webhook.fetch()
                try:
                    await webhook.delete(reason="Désactivation du message board")
                except discord.NotFound:
                    pass
                self.data.set_keyvalue_table_value(interaction.guild, 'settings', 'Webhook_URL', '')
                await interaction.followup.send("**Salon du message board** • Message board désactivé et webhook supprimé.", ephemeral=True)  
        else:
            if current_webhook_url:
                webhook = discord.Webhook.from_url(current_webhook_url, client=self.bot)
                webhook = await webhook.fetch()
                try:
                    await webhook.delete(reason="Changement du salon du message board")
                except discord.NotFound:
                    pass
                
                webhook = await channel.create_webhook(name="Message board", reason="Changement du salon du message board")
                self.data.set_keyvalue_table_value(interaction.guild, 'settings', 'Webhook_URL', webhook.url)
                await interaction.followup.send(f"**Salon du message board** • Message board déplacé dans <#{channel.id}>.", ephemeral=True)
            else:
                try:
                    webhook = await channel.create_webhook(name="Message board", reason="Activation du message board")
                except discord.Forbidden:
                    return await interaction.followup.send(f"**Salon du message board** • Je n'ai pas la permission de créer un webhook dans <#{channel.id}>.", ephemeral=True)
                self.data.set_keyvalue_table_value(interaction.guild, 'settings', 'Webhook_URL', webhook.url)
                await interaction.followup.send(f"**Salon du message board** • Message board activé dans <#{channel.id}>.", ephemeral=True)  
                
    @config_group.command(name='threshold')
    @app_commands.rename(threshold='seuil', half_notification='notif_moitie')
    async def set_msgboard_threshold(self, interaction: Interaction, threshold: app_commands.Range[int, 1], half_notification: bool = False):
        """Définit le seuil de votes pour qu'un message soit reposté
        
        :param threshold: Seuil pour qu'un message soit reposté
        :param half_notification: True pour activer la notification à la moitié du seuil"""
        if not isinstance(interaction.guild, discord.Guild):
            raise TypeError("La commande doit être utilisée sur un serveur.")
        
        if not self.data.get_keyvalue_table_value(interaction.guild, 'settings', 'Enabled', cast=bool):
            return await interaction.response.send_message("**Erreur** • Activez d'abord le message board avec `/msgboard enable`.", ephemeral=True)
        
        if half_notification and threshold < 3:
            return await interaction.response.send_message("**Conflit** • Le seuil pour reposter ne peut être inférieur à 3 lorsque la notification à la moitié du seuil est activée.", ephemeral=True)
        
        self.data.set_keyvalue_table_value(interaction.guild, 'settings', 'Threshold', threshold)
        self.data.set_keyvalue_table_value(interaction.guild, 'settings', 'NotifyHalfThreshold', int(half_notification))
        await interaction.response.send_message(f"**Succès** • Le seuil pour reposter a été défini à {threshold}{' et la notification à la moitié du seuil a été activée' if half_notification else ''}.", ephemeral=True)
        
    @config_group.command(name='emoji')
    async def set_msgboard_emoji(self, interaction: Interaction, emoji: str):
        """Modifier l'emoji utilisé pour voter

        :param emoji: Emoji à utiliser
        """
        if not isinstance(interaction.guild, discord.Guild):
            raise ValueError("L'interaction doit être sur un serveur.")
        
        if not self.data.get_keyvalue_table_value(interaction.guild, 'settings', 'Enabled', cast=bool):
            return await interaction.response.send_message("**Erreur** • Activez d'abord le message board avec `/msgboard enable`.", ephemeral=True)
        
        if type(emoji) is not str or len(emoji) > 1:
            return await interaction.response.send_message("**Erreur** · L'emoji doit être un emoji unicode de base.", ephemeral=True)
        
        self.data.set_keyvalue_table_value(interaction.guild, 'settings', 'Emoji', emoji)
        await interaction.response.send_message(f"**Emoji de vote** • Emoji de vote mis à jour : {emoji}.", ephemeral=True)
        
    @config_group.command(name='maxage')
    @app_commands.rename(maxage='age_max')
    async def set_msg_maxage(self, interaction: Interaction, maxage: app_commands.Range[int, 1, 72]):
        """Définit l'âge maximal que ne doit pas dépasser un message pour être reposté

        :param maxage: Âge maximal en heures
        """
        if not isinstance(interaction.guild, discord.Guild):
            raise ValueError("L'interaction doit être sur un serveur.")
        
        if not self.data.get_keyvalue_table_value(interaction.guild, 'settings', 'Enabled', cast=bool):
            return await interaction.response.send_message("**Erreur** • Activez d'abord le message board avec `/msgboard enable`.", ephemeral=True)
        
        self.data.set_keyvalue_table_value(interaction.guild, 'settings', 'MaxMessageAge', maxage * 60 * 60)
        await interaction.response.send_message(f"**Âge maximal** • L'âge maximal des messages a été défini à {maxage} heures.", ephemeral=True)
        
async def setup(bot):
    await bot.add_cog(MsgBoard(bot))
