import logging
from datetime import datetime, timedelta

import discord
from discord import Interaction, app_commands
from discord.ext import commands

from common import dataio

logger = logging.getLogger(f'NERON.{__name__.split(".")[-1]}')

COOLDOWN_DELAY = 60 * 60 * 6  # 6 heures

class SendModal(discord.ui.Modal):
    message_content = discord.ui.TextInput(label='Contenu',
                                           style=discord.TextStyle.long,
                                           placeholder='Contenu de votre message anonyme',
                                           required=True,
                                           min_length=1,
                                           max_length=1800)
    signature = discord.ui.TextInput(label='Signature',
                                     style=discord.TextStyle.short,
                                     placeholder='Signature (optionnelle) de votre message',
                                     required=False,
                                     max_length=100)
    def __init__(self, cog: 'Secrets', receiver: discord.User | discord.Member) -> None:
        super().__init__(title=f'Envoyer anonymement à {receiver.name}', timeout=600)
        self.__cog = cog
        self.receiver = receiver
    
    async def on_submit(self, interaction: discord.Interaction) -> None:
        msg = self.message_content.value
        if self.signature.value:
            msg += f"\n\n— *Message anonyme signé **{self.signature.value}***"
        else:
            msg += "\n\n— *Message anonyme*"
        try:
            sended = await self.receiver.send(msg)
        except discord.Forbidden:
            await interaction.response.send_message("**Erreur** • Je ne peux pas envoyer de message à cet utilisateur.", ephemeral=True)
        else:
            await interaction.response.send_message(f"**Message envoyé** • Votre message `#{sended.id}` a été envoyé à {self.receiver.mention}.", ephemeral=True)
            self.__cog.add_tracking(sended, interaction.user)
            self.__cog._cooldowns[(interaction.user.id, self.receiver.id)] = datetime.now() + timedelta(seconds=COOLDOWN_DELAY)

class Secrets(commands.Cog):
    """Envoi et réception de messages anonymes."""
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.data = dataio.get_cog_data(self)

        # Table de correspondance entre les messages et les utilisateurs pour pouvoir les bloquer
        tracking = dataio.TableInitializer(
            table_name="tracking",
            create_query="""CREATE TABLE IF NOT EXISTS tracking (
                message_id INTEGER PRIMARY KEY,
                sender_id INTEGER
                )"""
        )
        # Blacklistes d'utilisateurs
        blacklist = dataio.TableInitializer(
            table_name="blacklist",
            create_query="""CREATE TABLE IF NOT EXISTS blacklist (
                owner_id INTEGER PRIMARY KEY,
                blocked_ids TEXT
                )"""
        )
        self.data.append_initializers_for("global", [tracking, blacklist])
        
        self.anonymous_ctx = app_commands.ContextMenu(
            name='Envoyer anonymement',
            callback=self.send_anonymous_message,
            extras={'description': "Envoie un message anonyme à l'utilisateur visé."})
        self.bot.tree.add_command(self.anonymous_ctx)
        
        self._cooldowns : dict[tuple[int, int], datetime] = {}
    
    def cog_unload(self):
        self.data.close_all()
        
    # Tracking ----------------------------------------------------------------
    
    def add_tracking(self, message: discord.Message, sender: discord.User | discord.Member):
        """Ajoute un message à la table de tracking."""
        self.data.get('global').execute("INSERT INTO tracking VALUES (?, ?)", (message.id, sender.id))
        
    def get_tracking(self, message_id: int) -> int | None:
        """Renvoie l'utilisateur ayant envoyé le message."""
        r = self.data.get('global').fetchone("SELECT sender_id FROM tracking WHERE message_id = ?", (message_id,))
        if r:
            return r['sender_id']
        return None
    
    # Blacklist ----------------------------------------------------------------
    
    def add_blacklist(self, owner: discord.User | discord.Member, blocked: discord.User | discord.Member):
        """Ajoute un utilisateur à la blacklist d'un autre."""
        blocked_ids = self.data.get('global').fetchone("SELECT blocked_ids FROM blacklist WHERE owner_id = ?", (owner.id,))
        if blocked_ids:
            blocked_ids = set(map(int, blocked_ids['blocked_ids'].split(',') if blocked_ids['blocked_ids'] else []))
        else:
            blocked_ids = set()
        blocked_ids.add(blocked.id)
        self.data.get('global').execute("INSERT OR REPLACE INTO blacklist VALUES (?, ?)", (owner.id, ",".join(map(str, blocked_ids))))
        
    def remove_blacklist(self, owner: discord.User | discord.Member, blocked: discord.User | discord.Member):
        """Retire un utilisateur de la blacklist d'un autre."""
        blocked_ids = self.data.get('global').fetchone("SELECT blocked_ids FROM blacklist WHERE owner_id = ?", (owner.id,))
        if blocked_ids:
            blocked_ids = set(map(int, blocked_ids['blocked_ids'].split(',')))
            blocked_ids.discard(blocked.id)
            if blocked_ids:
                self.data.get('global').execute("INSERT OR REPLACE INTO blacklist VALUES (?, ?)", (owner.id, ",".join(map(str, blocked_ids))))
            else:
                self.data.get('global').execute("DELETE FROM blacklist WHERE owner_id = ?", (owner.id,))
            
    def get_blacklist(self, owner: discord.User | discord.Member) -> set[int]:
        """Renvoie la liste des utilisateurs bloqués par un autre."""
        blocked_ids = self.data.get('global').fetchone("SELECT blocked_ids FROM blacklist WHERE owner_id = ?", (owner.id,))
        if blocked_ids:
            return set(map(int, blocked_ids['blocked_ids'].split(',')))
        return set()
        
    async def send_anonymous_message(self, interaction: Interaction, user: discord.User | discord.Member):
        """Envoie un message anonymisé en MP à un utilisateur."""
        author = interaction.user
        if user.id == author.id:
            return await interaction.response.send_message(f"**Impossible** • Vous ne pouvez pas vous envoyer de message à vous-même.", ephemeral=True)
        if user.bot:
            return await interaction.response.send_message(f"**Impossible** • Vous ne pouvez pas envoyer de message à un bot.", ephemeral=True)
        if author.id in self.get_blacklist(user):
            return await interaction.response.send_message(f"**Impossible** • Cet utilisateur a bloqué un de vos messages, vous ne pouvez donc plus lui en envoyer.", ephemeral=True)
        cd = self._cooldowns.get((author.id, user.id))
        if cd and datetime.now() < cd:
            return await interaction.response.send_message(f"**Cooldown** • Vous devez attendre {cd.strftime('%Hh%M')} pour renvoyer au même utilisateur.", ephemeral=True)
        
        modal = SendModal(self, user)
        await interaction.response.send_modal(modal)
        
    # COMMANDES ----------------------------------------------------------------
    
    secrets_group = app_commands.Group(name='secrets', description="Commandes liées aux messages anonymes.")
    
    @secrets_group.command(name='block')
    async def block_user(self, interaction: Interaction, message_id: str):
        """Bloque un utilisateur pour ne plus recevoir de messages anonymes de sa part.
        
        :param message_id: L'identifiant du message dont il faut bloquer l'auteur"""
        msg_id = int(message_id)
        sender_id = self.get_tracking(msg_id)
        if sender_id:
            sender = self.bot.get_user(sender_id)
            if not sender:
                return await interaction.response.send_message(f"**Erreur** • L'utilisateur concerné n'est pas joignable.", ephemeral=True)
                
            blacklist = self.get_blacklist(interaction.user)
            if sender.id in blacklist:
                await interaction.response.send_message(f"**Utilisateur bloqué** • Vous avez déjà bloqué l'auteur de ce message.", ephemeral=True)
            else:
                self.add_blacklist(interaction.user, sender)
                await interaction.response.send_message(f"**Utilisateur bloqué** • Vous ne pourrez plus recevoir de messages anonymes de la part de l'auteur de ce message.", ephemeral=True)

    @secrets_group.command(name='unblock')
    async def unblock_user(self, interaction: Interaction, message_id: str):
        """Débloque un utilisateur pour recevoir de nouveau des messages anonymes de sa part.
        
        :param message_id: L'identifiant du message dont il faut débloquer l'auteur"""
        msg_id = int(message_id)
        sender_id = self.get_tracking(msg_id)
        if sender_id:
            sender = self.bot.get_user(sender_id)
            if not sender:
                return await interaction.response.send_message(f"**Erreur** • L'utilisateur concerné n'est pas joignable.", ephemeral=True)
                
            blacklist = self.get_blacklist(interaction.user)
            if sender.id in blacklist:
                self.remove_blacklist(interaction.user, sender)
                await interaction.response.send_message(f"**Utilisateur débloqué** • Vous pouvez de nouveau recevoir des messages anonymes de la part de l'auteur de ce message.", ephemeral=True)
            else:
                await interaction.response.send_message(f"**Utilisateur non bloqué** • Vous n'avez pas bloqué l'auteur de ce message.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Secrets(bot))
