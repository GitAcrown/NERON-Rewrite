import logging
import re
import textwrap
from datetime import datetime
from io import BytesIO
from typing import List, Optional, Tuple

import aiohttp
import colorgram
import discord
import numpy as np
from discord import Interaction, app_commands
from discord.components import SelectOption
from discord.ext import commands, tasks
from PIL import Image, ImageDraw, ImageFont

from common import dataio
from common.utils import interface, pretty

logger = logging.getLogger(f'NERON.{__name__.capitalize()}')

QUOTE_EXPIRATION = 60 * 60 * 24 * 30 # 30 jours

class QuotifyMessageSelect(discord.ui.Select):
    """Menu déroulant pour sélectionner les messages à citer"""
    def __init__(self, view: 'QuotifyView', placeholder: str, options: List[discord.SelectOption]):
        super().__init__(placeholder=placeholder, 
                         min_values=1, 
                         max_values=min(len(options), 5), 
                         options=options)
        self.__view = view
        
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        if sum([len(m.clean_content) for m in self.__view.selected_messages]) > 1000:
            return await interaction.followup.send("**Action impossible** · Le message est trop long", ephemeral=True)
        
        self.__view.selected_messages = [m for m in self.__view.potential_messages if m.id in [int(v) for v in self.values]]
        self.options = [SelectOption(label=f"{pretty.shorten_text(m.clean_content, 100)}", value=str(m.id), description=m.created_at.strftime('%H:%M %d/%m/%y'), default=str(m.id) in self.values) for m in self.__view.potential_messages]
        image = await self.__view._get_image()
        if not image:
            return await interaction.followup.send("**Erreur** · Impossible de créer l'image de la citation", ephemeral=True)
        await interaction.edit_original_response(view=self.__view, attachments=[image])


class QuotifyView(discord.ui.View):
    """Menu de création de citation afin de sélectionner les messages à citer"""
    def __init__(self, cog: 'Quotes', initial_message: discord.Message, *, timeout: float | None = 15):
        super().__init__(timeout=timeout)
        self.__cog = cog
        self.initial_message = initial_message
        self.potential_messages = []
        self.selected_messages = [initial_message]
        
        self.interaction : Interaction | None = None
        
    async def interaction_check(self, interaction: discord.Interaction):
        if not self.interaction:
            return False
        if interaction.user != self.interaction.user:
            await interaction.response.send_message("**Action impossible** · Seul l'auteur du message initial peut utiliser ce menu", ephemeral=True)
            return False
        return True
    
    async def on_timeout(self):
        new_view = discord.ui.View()
        message_url = self.selected_messages[0].jump_url
        new_view.add_item(discord.ui.Button(label="Source", url=message_url, style=discord.ButtonStyle.link))
        
        msg = None
        if self.interaction:
            msg = await self.interaction.edit_original_response(view=new_view)
            
        if msg:
            image_url = msg.attachments[0].url
            self.log_generated_quote(' '.join([m.content for m in self.selected_messages]), image_url, self.selected_messages[0].jump_url, self.selected_messages[0].author.id)
            
    async def start(self, interaction: discord.Interaction):
        await interaction.response.defer()
        
        potential_msgs = await self.__cog.fetch_following_messages(self.initial_message)
        self.potential_messages = sorted(potential_msgs, key=lambda m: m.created_at)
        if len(self.potential_messages) > 1:
            options = [SelectOption(label=f"{pretty.shorten_text(m.clean_content, 100)}", value=str(m.id), description=m.created_at.strftime('%H:%M %d/%m/%y'), default= m == self.initial_message) for m in self.potential_messages]
            self.add_item(QuotifyMessageSelect(self, "Sélectionnez les messages à citer", options))
        
        image = await self._get_image()
        if not image:
            return await interaction.followup.send("**Erreur** · Impossible de créer l'image de la citation", ephemeral=True)
        await interaction.followup.send(view=self, file=image)
        self.interaction = interaction

    async def _get_image(self) -> Optional[discord.File]:
        try:
            return await self.__cog.generate_quote_from(self.selected_messages)
        except Exception as e:
            logger.exception(e)
            if self.interaction:
                await self.interaction.edit_original_response(content=str(e), view=None)
            return None
        
    def log_generated_quote(self, full_content: str, generated_image_url: str, message_url: str, author_id: int):
        guild = self.interaction.guild if self.interaction else None
        if not guild:
            return
        self.__cog.log_quote(guild, full_content, generated_image_url, message_url, author_id)
        
    @discord.ui.button(label="Enregistrer", style=discord.ButtonStyle.green, row=1)
    async def save_quit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        new_view = discord.ui.View()
        message_url = self.selected_messages[0].jump_url
        new_view.add_item(discord.ui.Button(label="Aller au message", url=message_url, style=discord.ButtonStyle.link))
        
        msg = None
        if self.interaction:
            msg = await self.interaction.edit_original_response(view=new_view)
        
        if msg:
            image_url = msg.attachments[0].url
            self.log_generated_quote(' '.join([m.content for m in self.selected_messages]), image_url, self.selected_messages[0].jump_url, self.selected_messages[0].author.id)
            
        self.stop()
        
    @discord.ui.button(label="Annuler", style=discord.ButtonStyle.red, row=1)
    async def quit(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.defer()
        if self.interaction:
            await self.interaction.delete_original_response()
            

class Quotes(commands.Cog):
    """Citations créées ou obtenues avec Inspirobot.me"""
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.data = dataio.get_cog_data(self)

        self.generate_quote = app_commands.ContextMenu(
            name='Générer une citation',
            callback=self.generate_quote_callback, 
            extras={'description': "Génère une image de citation avec le contenu du message sélectionné."})
        self.bot.tree.add_command(self.generate_quote)
        
        quote_logs = dataio.ObjectTableInitializer(
            table_name='quote_logs',
            create_query="""CREATE TABLE IF NOT EXISTS quote_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT,
                image_url TEXT,
                message_url TEXT,
                author_id INTEGER,
                timestamp INTEGER
                )""")
        self.data.register_tables_for(discord.Guild, [quote_logs])
        
        self.__assets = self.__load_assets()
        
    def cog_unload(self):
        self.data.close_all()
        
    def __load_assets(self) -> dict:
        assets = {}
        assets_path = self.data.get_folder('assets')
        assets['quotemark_white'] = Image.open(str(assets_path / 'quotemark_white.png')).convert('RGBA')
        assets['quotemark_black'] = Image.open(str(assets_path / 'quotemark_black.png')).convert('RGBA')
        return assets
    
    # Nettoyage des citations --------------------------------------------------
    
    @tasks.loop(hours=24)
    async def clean_quotes(self):
        """Nettoie les citations générées il y a plus d'un mois"""
        for guild in self.bot.guilds:
            if not isinstance(guild, discord.Guild):
                continue
            query = """DELETE FROM quote_logs WHERE timestamp < ?"""
            self.data.get(guild).execute(query, (int(datetime.now().timestamp()) - QUOTE_EXPIRATION,))
        
    # Callback ------------------------------------------------------------------
    
    async def generate_quote_callback(self, interaction: Interaction, message: discord.Message):
        """Callback pour la commande de génération de citation"""
        if not message.content or message.content.isspace():
            return await interaction.response.send_message("**Action impossible** · Le message est vide", ephemeral=True)
        if interaction.channel_id != message.channel.id:
            return await interaction.response.send_message("**Action impossible** · Le message doit être dans le même salon", ephemeral=True)
        
        try:
            view = QuotifyView(self, message)
            await view.start(interaction)
        except Exception as e:
            logger.exception(e)
            await interaction.response.send_message(f"**Erreur dans l'initialisation du menu** · {e}", ephemeral=True)
        
    # Génération de citations -------------------------------------------------
    
    def _add_gradient(self, image: Image.Image, gradient_magnitude=1.0, color: Tuple[int, int, int]=(0, 0, 0)):
        im = image
        if im.mode != 'RGBA':
            im = im.convert('RGBA')
        width, height = im.size
        y, _ = np.indices((height, width))
        alpha = (gradient_magnitude * y / height * 255).astype(np.uint8)
        alpha = np.minimum(alpha, 255)
        black_im = Image.new('RGBA', (width, height), color=color)
        black_im.putalpha(Image.fromarray(alpha))
        gradient_im = Image.alpha_composite(im, black_im)
        return gradient_im
    
    def create_quote_image(self, avatar: str | BytesIO, text: str, author_name: str, date: str, *, size: tuple[int, int] = (512, 512)):
        """Crée une image de citation avec un avatar, un texte, un nom d'auteur et une date."""
        text = text.upper()

        w, h = size
        box_w, _ = int(w * 0.92), int(h * 0.72)
        image = Image.open(avatar).convert("RGBA").resize(size)

        assets_path = self.data.get_folder('assets')
        font_path = str(assets_path / "NotoBebasNeue.ttf")
        bg_color = colorgram.extract(image.resize((50, 50)), 1)[0].rgb 
        grad_magnitude = 0.85 + 0.05 * (len(text) // 100)
        image = self._add_gradient(image, grad_magnitude, bg_color)
        luminosity = (0.2126 * bg_color[0] + 0.7152 * bg_color[1] + 0.0722 * bg_color[2]) / 255

        text_size = int(h * 0.08)
        text_font = ImageFont.truetype(font_path, text_size, encoding='unic')
        draw = ImageDraw.Draw(image)
        text_color = (255, 255, 255) if luminosity < 0.5 else (0, 0, 0)

        # Texte principal --------
        max_lines = len(text) // 60 + 2 if len(text) > 200 else 4
        wrap_width = int(box_w / (text_font.getlength("A") * 0.85))
        lines = textwrap.fill(text, width=wrap_width, max_lines=max_lines, placeholder="§")
        while lines[-1] == "§":
            text_size -= 2
            text_font = ImageFont.truetype(font_path, text_size, encoding='unic')
            wrap_width = int(box_w / (text_font.getlength("A") * 0.85))
            lines = textwrap.fill(text, width=wrap_width, max_lines=max_lines, placeholder="§")
        draw.multiline_text((w / 2, h * 0.835), lines, font=text_font, spacing=0.25, align='center', fill=text_color, anchor='md')

        # Icone et lignes ---------
        icon = self.__assets['quotemark_white'] if luminosity < 0.5 else self.__assets['quotemark_black']
        icon_image = icon.resize((int(w * 0.06), int(w * 0.06)))
        icon_left = w / 2 - icon_image.width / 2
        image.paste(icon_image, (int(icon_left), int(h * 0.85 - icon_image.height / 2)), icon_image)

        author_font = ImageFont.truetype(font_path, int(h * 0.060), encoding='unic')
        draw.text((w / 2,  h * 0.95), author_name, font=author_font, fill=text_color, anchor='md', align='center')

        draw.line((icon_left - w * 0.25, h * 0.85, icon_left - w * 0.02, h * 0.85), fill=text_color, width=1) # Ligne de gauche
        draw.line((icon_left + icon_image.width + w * 0.02, h * 0.85, icon_left + icon_image.width + w * 0.25, h * 0.85), fill=text_color, width=1) # Ligne de droite

        # Date -------------------
        date_font = ImageFont.truetype(font_path, int(h * 0.040), encoding='unic')
        draw.text((w / 2,  h * 0.985), date, font=date_font, fill=text_color, anchor='md', align='center')

        return image
    
    async def fetch_following_messages(self, starting_message: discord.Message, messages_limit: int = 5, lenght_limit: int = 1000) -> list[discord.Message]:
        """Ajoute au message initial les messages suivants jusqu'à atteindre la limite de caractères ou de messages"""
        messages = [starting_message]
        total_length = len(starting_message.content)
        async for message in starting_message.channel.history(limit=25, after=starting_message):
            if not message.content or message.content.isspace():
                continue
            if message.author != starting_message.author:
                continue
            total_length += len(message.content)
            if total_length > lenght_limit:
                break
            messages.append(message)
            if len(messages) >= messages_limit:
                break
        return messages
    
    def normalize_text(self, text: str) -> str:
        """Effectue des remplacements de texte pour éviter les problèmes d'affichage"""
        text = re.sub(r'<a?:(\w+):\d+>', r':\1:', text)
        text = re.sub(r'(\*|_|`|~|\\)', r'', text)
        return text
    
    async def generate_quote_from(self, messages: list[discord.Message]) -> discord.File:
        messages = sorted(messages, key=lambda m: m.created_at)
        base_message = messages[0]
        if not isinstance(base_message.author, discord.Member):
            raise ValueError("Le message de base doit être envoyé par un membre du serveur.")
        
        avatar = BytesIO(await messages[0].author.display_avatar.read())
        message_date = messages[0].created_at.strftime("%d.%m.%Y")
        full_content = ' '.join([self.normalize_text(m.content) for m in messages])
        author_name = f"@{base_message.author.name}" if not base_message.author.nick else f"{base_message.author.nick} (@{base_message.author.name})"
        try:
            image = self.create_quote_image(avatar, full_content, author_name, message_date, size=(650, 650))
        except Exception as e:
            logger.exception(e, exc_info=True)
            raise ValueError("Impossible de générer l'image de citation.")
        
        with BytesIO() as buffer:
            image.save(buffer, format='PNG')
            buffer.seek(0)
            alt_text = f"\"{full_content}\" - {author_name} ({message_date})"
            return discord.File(buffer, filename='quote.png', description=alt_text)
        
            
    # Logs de citations générées ----------------------------------------------
    
    def log_quote(self, guild: discord.Guild, full_content: str, generated_image_url: str, message_url: str, author_id: int):
        """Enregistre une citation générée dans la base de données"""
        query = """INSERT INTO quote_logs (content, image_url, message_url, author_id, timestamp) VALUES (?, ?, ?, ?, ?)"""
        self.data.get(guild).execute(query, (full_content, generated_image_url, message_url, author_id, int(datetime.now().timestamp())))
        
    def get_quote_logs(self, guild: discord.Guild, limit: int = 20) -> list[dict]:
        """Récupère les logs de citations générées"""
        query = """SELECT * FROM quote_logs ORDER BY timestamp DESC LIMIT ?"""
        r = self.data.get(guild).fetchall(query, (limit,))
        return r if r else []
    
    # COMMANDES =================================================================
    
    @app_commands.command(name='quote')
    @app_commands.checks.cooldown(1, 600)
    async def fetch_inspirobot_quote(self, interaction: Interaction):
        """Obtenir une citation aléatoire de Inspirobot.me"""
        await interaction.response.defer()
        
        async def get_quote():
            async with aiohttp.ClientSession() as session:
                async with session.get('https://inspirobot.me/api?generate=true') as resp:
                    if resp.status != 200:
                        return None
                    return await resp.text()
                
        url = await get_quote()
        if url is None:
            return await interaction.followup.send("**Erreur** • Impossible d'obtenir une citation depuis Inspirobot.me.", ephemeral=True)
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return await interaction.followup.send("**Erreur** • Impossible d'obtenir une citation depuis Inspirobot.me.", ephemeral=True)
                data = BytesIO(await resp.read())
        
        await interaction.followup.send(file=discord.File(data, 'quote.png', description="Citation fournie par Inspirobot.me"))
        
    @app_commands.command(name='lastquotes')
    @app_commands.guild_only()
    async def get_last_quotes(self, interaction: Interaction):
        """Affiche les 20 dernières citations générées sur le serveur"""
        if not isinstance(interaction.guild, discord.Guild):
            return await interaction.response.send_message("**Action impossible** • Cette commande n'est pas disponible en message privé.", ephemeral=True)
        
        await interaction.response.defer()
        logs = self.get_quote_logs(interaction.guild)
        if not logs:
            return await interaction.followup.send("**Aucune citation générée** • Aucune citation n'a été générée sur ce serveur.", ephemeral=True)

        embeds = []
        for log in logs:
            author = interaction.guild.get_member(log['author_id'])
            if not author:
                continue
            embed = discord.Embed(color=pretty.DEFAULT_EMBED_COLOR, timestamp=datetime.fromtimestamp(log['timestamp']))
            embed.set_author(name=f"{author.name}", url=str(log['message_url']), icon_url=author.display_avatar.url)
            embed.set_image(url=log['image_url'])
            embed.set_footer(text=f"ID: {log['id']}")
            embeds.append(embed)
            
        view = interface.EmbedPaginatorMenu(embeds=embeds, users=[interaction.user], timeout=30)
        await view.start(interaction)

async def setup(bot):
    await bot.add_cog(Quotes(bot))
