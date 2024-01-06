import asyncio
import colorsys
import logging
import re
from io import BytesIO
from typing import Iterable

import aiohttp
import colorgram
import discord
from discord import Interaction, app_commands
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont, ImageOps

from common import dataio
from common.utils import pretty

logger = logging.getLogger(f'NERON.{__name__.split(".")[-1]}')

INVALID_COLOR = 0x000000 # Utilisé par Discord pour les rôles sans couleur
INVALID_NAME = '#000000' # Utilisé par Discord pour les rôles sans couleur
COLOR_ROLE_NAME_PATTERN = r'^#?([0-9a-fA-F]{6})$' # ex. #ff0000
CLEANUP_COUNTDOWN = 10 # Nombre de changements de rôle avant de faire du ménage

class AvatarPreviewSelectMenu(discord.ui.View):
    def __init__(self, initial_interaction: Interaction, previews: list[tuple[Image.Image, str]], *, timeout: float = 60):
        super().__init__(timeout=timeout)
        self.initial_interaction = initial_interaction
        self.previews = previews
        
        self.current_page = 0
        
        self.result = None
        
    async def interaction_check(self, interaction: Interaction) -> bool:
        if interaction.user != self.initial_interaction.user:
            await interaction.response.send_message("Seul l'utilisateur ayant lancé la commande peut utiliser ce menu.", ephemeral=True)
            return False
        return True
    
    def get_embed(self) -> discord.Embed:
        current_color = self.previews[self.current_page][1]
        em = discord.Embed(title=f"Preview du rôle • {current_color}", color=discord.Color(int(current_color[1:], 16)))
        em.set_image(url="attachment://preview.png")
        em.set_footer(text=f"Page {self.current_page + 1}/{len(self.previews)}")
        return em
    
    async def on_timeout(self) -> None:
        self.stop()
        await self.initial_interaction.delete_original_response()
        
    async def start(self) -> None:
        with BytesIO() as buffer:
            self.previews[self.current_page][0].save(buffer, format='PNG')
            buffer.seek(0)
            await self.initial_interaction.followup.send(embed=self.get_embed(), file=discord.File(buffer, filename='preview.png', description="Preview"), view=self)

    async def update(self) -> None:
        with BytesIO() as buffer:
            self.previews[self.current_page][0].save(buffer, format='PNG')
            buffer.seek(0)
            await self.initial_interaction.edit_original_response(embed=self.get_embed(), attachments=[discord.File(buffer, filename='preview.png', description="Preview")])

    # Boutons ------------------------------------------------------------------
    
    @discord.ui.button(style=discord.ButtonStyle.grey, emoji=pretty.DEFAULT_ICONS_EMOJIS['back'])
    async def previous_button(self, interaction: Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        self.current_page -= 1
        if self.current_page < 0:
            self.current_page = len(self.previews) - 1
        await self.update()
        
    @discord.ui.button(label='Annuler', style=discord.ButtonStyle.red)
    async def stop_button(self, interaction: Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        self.stop()
        await interaction.delete_original_response()
        
    @discord.ui.button(label='Appliquer', style=discord.ButtonStyle.green)
    async def choose_button(self, interaction: Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        self.result = self.previews[self.current_page][1]
        self.stop()
        await interaction.edit_original_response(view=None)
        
    @discord.ui.button(style=discord.ButtonStyle.grey, emoji=pretty.DEFAULT_ICONS_EMOJIS['next'])
    async def next_button(self, interaction: Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        self.current_page += 1
        if self.current_page >= len(self.previews):
            self.current_page = 0
        await self.update()
        

class Colors(commands.Cog):
    """Système de distribution de rôles de couleur."""
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.data = dataio.get_cog_data(self)
        
        default_settings = {
            'Enabled': 0,
            'MasterRoleID': 0 # Rôle utilisé pour ordonner les rôles de couleur dans la liste des rôles
        }
        self.data.register_keyvalue_table_for(discord.Guild, 'settings', default_values=default_settings)
        
        self.reorganize_countdowns = {}

    def cog_unload(self):
        self.data.close_all()
        
    # Paramètres de serveur ----------------------------------------------------
    
    def is_enabled(self, guild: discord.Guild) -> bool:
        """Renvoie True si le système de rôles de couleur est activé sur le serveur."""
        return self.data.get_keyvalue_table_value(guild, 'settings', 'Enabled', cast=bool)
    
    def get_master_role(self, guild: discord.Guild) -> discord.Role | None:
        """Renvoie le rôle utilisé pour ordonner les rôles de couleur dans la liste des rôles."""
        role_id = self.data.get_keyvalue_table_value(guild, 'settings', 'MasterRoleID', cast=int)
        return guild.get_role(role_id)
    
    def set_master_role(self, guild: discord.Guild, role: discord.Role | None) -> None:
        """Définit le rôle utilisé pour ordonner les rôles de couleur dans la liste des rôles."""
        self.data.set_keyvalue_table_value(guild, 'settings', 'MasterRoleID', role.id if role else 0)
        
    # Rôles de couleur ---------------------------------------------------------
    
    def get_color_roles(self, guild: discord.Guild) -> list[discord.Role]:
        """Renvoie la liste des rôles de couleur du serveur."""
        return [r for r in guild.roles if r.name.startswith('#') and r.name[1:].isalnum() and f'#{r.name[1:].lower()}' != INVALID_NAME]
    
    def get_color_role(self, guild: discord.Guild, hex_color: str) -> discord.Role | None:
        """Renvoie le rôle de couleur correspondant à la couleur donnée."""
        return discord.utils.get(self.get_color_roles(guild), name=f'#{hex_color}')
    
    def get_member_color_role(self, member: discord.Member) -> discord.Role | None:
        """Renvoie le rôle de couleur du membre."""
        return discord.utils.find(lambda r: r in member.roles, self.get_color_roles(member.guild))
    
    def can_be_recycled(self, role: discord.Role, ignore: Iterable[discord.Member]) -> bool:
        """Renvoie True si le rôle peut être recyclé (changements de nom et de couleur)."""
        return not any([m for m in role.members if m not in ignore])
    
    def is_color_displayed(self, member: discord.Member) -> bool:
        """Renvoie True si la couleur du membre est affichée."""
        user_color = self.get_member_color_role(member)
        if not user_color:
            return False
        
        color_roles = sorted(
            [r for r in member.roles if r.color.value != INVALID_COLOR],
            key=lambda r: r.position
        )
        return color_roles[0] == user_color # Si le rôle le plus haut est le rôle de couleur du membre
    
    async def fetch_color_role(self, guild: discord.Guild, hex_color: str, requester: discord.Member) -> discord.Role | None:
        """Renvoie le rôle de couleur correspondant à la couleur donnée. Crée ou recycle un rôle au besoin."""
        # On vérifie si la couleur est valide
        if not re.match(COLOR_ROLE_NAME_PATTERN, hex_color):
            return None
        color = discord.Color(int(hex_color, 16))
        
        # On vérifie si le rôle existe déjà
        role = self.get_color_role(guild, hex_color)
        if role:
            return role
        
        # On vérifie si le rôle du membre peut être recyclé
        user_color = self.get_member_color_role(requester)
        if user_color and self.can_be_recycled(user_color, [requester]):
            await user_color.edit(name=f'#{hex_color.upper()}', color=color, reason=f"Recyclé pour {requester}")
            return user_color
        
        # On vérifie si un autre rôle peut être recyclé
        for role in self.get_color_roles(guild):
            if self.can_be_recycled(role, [requester]):
                await role.edit(name=f'#{hex_color.upper()}', color=color, reason=f"Recyclé pour {requester}")
                return role
            
        # Sinon, on crée un nouveau rôle
        role = await guild.create_role(name=f'#{hex_color.upper()}', color=color, reason=f"Créé pour {requester}")
        
        countdown = self.reorganize_countdowns.get(guild.id, CLEANUP_COUNTDOWN)
        if not countdown: # Tous les CLEANUP_COUNTDOWN changements de rôle on fait du ménage
            await self.clean_unused_color_roles(guild)
            await self.reorganize_color_roles(guild)
            self.reorganize_countdowns[guild.id] = CLEANUP_COUNTDOWN
        else:
            await self.move_role(role)
            self.reorganize_countdowns = countdown - 1
        return role

    async def move_role(self, role: discord.Role, *, position: int = 0) -> None:
        """Déplace le rôle à la position donnée."""
        master_role = self.get_master_role(role.guild)
        if master_role and master_role.position > position:
            position = master_role.position - 1
        await role.edit(position=position)
        
    async def reorganize_color_roles(self, guild: discord.Guild) -> None:
        """Réorganise les rôles de couleur du serveur sous le rôle maître."""
        master_role = self.get_master_role(guild)
        if not master_role:
            return
        
        color_roles = self.get_color_roles(guild)
        # On les range dans l'ordre des couleurs de l'arc-en-ciel
        color_roles.sort(key=lambda r: self.rgb_to_hsv(r.name))
        try:
            await guild.edit_role_positions({r: master_role.position - (i + 1) for i, r in enumerate(color_roles)})
        except Exception as e:
            logger.exception(e, exc_info=True)
            raise commands.CommandError("Impossible de réorganiser les rôles de couleur, vérifiez que j'ai la permission de gérer les rôles.")
        
    async def clean_unused_color_roles(self, guild: discord.Guild) -> int:
        """Supprime les rôles de couleur inutilisés du serveur et renvoie le nombre de rôles supprimés."""
        color_roles = self.get_color_roles(guild)
        count = 0
        for role in color_roles:
            if self.can_be_recycled(role, []):
                await role.delete(reason="Rôle de couleur inutilisé")
                count += 1
        return count
                
    # Opérations de couleurs ---------------------------------------------------
    
    def draw_image_palette(self, img: str | BytesIO, n_colors: int = 5) -> Image.Image:
        """Dessine une palette de couleurs à partir d'une image."""
        assets_path = str(self.data.get_folder('assets'))
        try:
            image = Image.open(img).convert('RGB')
        except Exception as e:
            raise commands.CommandError("Impossible d'ouvrir l'image.")
        colors = colorgram.extract(image.resize((100, 100)), n_colors)

        image = ImageOps.contain(image, (500, 500), method=Image.LANCZOS)
        iw, ih = image.size
        w, h = (iw + 100, ih)
        font = ImageFont.truetype(f'{assets_path}/RobotoRegular.ttf', 18)
        palette = Image.new('RGB', (w, h), color='white')
        maxcolors = h // 30
        colors = colors[:maxcolors] if len(colors) > maxcolors else colors
        blockheight = h // len(colors)

        draw = ImageDraw.Draw(palette)
        for i, color in enumerate(colors):
            if i == len(colors) - 1:
                block = (iw, i * blockheight, iw + 100, h)
            else:
                block = (iw, i * blockheight, iw + 100, i * blockheight + blockheight)
            palette.paste(color.rgb, block)
            hex_color = f'#{color.rgb.r:02x}{color.rgb.g:02x}{color.rgb.b:02x}'.upper()
            text_color = 'white' if color.rgb[0] + color.rgb[1] + color.rgb[2] < 384 else 'black'
            draw.text((iw + 10, i * blockheight + 10), hex_color, font=font, fill=text_color)

        palette.paste(image, (0, 0))
        return palette
    
    async def draw_discord_emulation(self, member: discord.Member, *, limit: int = 5) -> list[tuple[Image.Image, str]]:
        """Dessine des simulations des couleurs des rôles depuis l'avatar du membre sur Discord.
        
        Renvoie une liste de tuples (image, couleur) où image est une image de prévisualisation et couleur est la couleur correspondante."""
        assets_path = str(self.data.get_folder('assets'))
        avatar = await member.display_avatar.with_size(128).read()
        avatar = Image.open(BytesIO(avatar)).convert('RGBA')
        colors = colorgram.extract(avatar.resize((75, 75)), limit)

        mask = Image.new('L', avatar.size, 0)
        draw = ImageDraw.Draw(mask)
        draw.ellipse((0, 0) + avatar.size, fill=255)
        avatar.putalpha(mask)
        avatar = avatar.resize((46, 46), Image.LANCZOS)
        
        versions = []
        for name_color in [c for c in colors if f'#{c.rgb.r:02x}{c.rgb.g:02x}{c.rgb.b:02x}' != INVALID_NAME]:
            images = []
            name_font = ImageFont.truetype(f'{assets_path}/gg_sans.ttf', 18)
            content_font = ImageFont.truetype(f'{assets_path}/gg_sans_light.ttf', 18)
            for bg_color in [(0, 0, 0), (54, 57, 63), (255, 255, 255)]:
                bg = Image.new('RGBA', (420, 75), color=bg_color)
                bg.paste(avatar, (13, 13), avatar)
                d = ImageDraw.Draw(bg)
                # Nom
                d.text((76, 10), member.display_name, font=name_font, fill=name_color.rgb)
                # Contenu
                txt_color = (255, 255, 255) if bg_color in [(54, 57, 63), (0, 0, 0)] else (0, 0, 0)
                d.text((76, 34), f"Prévisualisation de l'affichage du rôle", font=content_font, fill=txt_color)
                images.append(bg)
            
            full = Image.new('RGBA', (420, 75 * 3))
            full.paste(images[0], (0, 0))
            full.paste(images[1], (0, 75))
            full.paste(images[2], (0, 75 * 2))
            versions.append((full, f'#{name_color.rgb.r:02x}{name_color.rgb.g:02x}{name_color.rgb.b:02x}'.upper()))
            
        return versions
        
    # Utilitaires --------------------------------------------------------------
    
    def rgb_to_hsv(self, hex_color: str) -> tuple[float, float, float]:
        hex_color = hex_color.lstrip("#")
        lh = len(hex_color)
        r, g, b = (int(hex_color[i:i + lh // 3], 16) / 255.0 for i in range(0, lh, lh // 3))
        return colorsys.rgb_to_hsv(r, g, b)
    
    # COMMANDES ================================================================
    
    @app_commands.command(name='palette')
    @app_commands.rename(n_colors='nb_couleurs', image_file='image', user='membre')
    async def palette_command(self, 
                              interaction: Interaction, 
                              n_colors: app_commands.Range[int, 3, 10] = 5, 
                              url: str | None = None, 
                              image_file: discord.Attachment | None = None, 
                              user: discord.Member | None = None) -> None:
        """Génère une palette de couleurs à partir d'une image
        
        :param n_colors: Nombre de couleurs à extraire (entre 3 et 10)
        :param url: URL de l'image
        :param image_file: Fichier image attaché
        :param user: Membre dont l'avatar est utilisé
        """
        await interaction.response.defer()
        img = None
        if image_file:
            img = BytesIO(await image_file.read()) 
        elif url:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        img = BytesIO(await resp.read())
                    else:
                        await interaction.followup.send("**Erreur** • Impossible de télécharger l'image depuis l'URL.", ephemeral=True)
        elif user:
            img = BytesIO(await user.display_avatar.read())
        elif isinstance(interaction.channel, (discord.TextChannel, discord.Thread, discord.DMChannel, discord.GroupChannel)):
            # On récumète la dernière image envoyée sur le salon (parmi les 10 derniers messages)
            async for message in interaction.channel.history(limit=10):
                if message.attachments:
                    img = BytesIO(await message.attachments[0].read())
                    break
    
        if not img:
            return await interaction.followup.send("**Erreur** • Aucune image n'a été fournie ni trouvée dans les 10 derniers messages.", ephemeral=True)
        
        try:
            palette = self.draw_image_palette(img, n_colors)
        except Exception as e:
            logger.exception(e, exc_info=True)
            return await interaction.followup.send("**Erreur** • Impossible de générer la palette de couleurs.", ephemeral=True)
        
        with BytesIO() as buffer:
            palette.save(buffer, format='PNG')
            buffer.seek(0)
            await interaction.followup.send(file=discord.File(buffer, filename='palette.png', description="Palette de couleurs générée"))
            
    
    mycolor_group = app_commands.Group(name='mycolor', description="Gestion de votre rôle de couleur", guild_only=True)
    
    @mycolor_group.command(name='get')
    @app_commands.rename(color='couleur')
    async def get_color_command(self, interaction: Interaction, color: str):
        """Obtenir un rôle de la couleur hexadécimale donnée
        
        :param color: Couleur hexadécimale (ex. #ff0123)
        """
        if not isinstance(interaction.guild, discord.Guild) or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("Cette commande n'est pas disponible en dehors des serveurs.", ephemeral=True)
        
        if not self.is_enabled(interaction.guild):
            return await interaction.response.send_message("**Non disponible** • Le système de rôles de couleur n'est pas activé sur ce serveur.", ephemeral=True)
        
        if not re.match(COLOR_ROLE_NAME_PATTERN, color):
            return await interaction.response.send_message("**Erreur** • La couleur donnée n'est pas au format hexadécimal (ex. #ff0123).", ephemeral=True)
        
        color = color.lstrip('#')
        if color == INVALID_NAME:
            return await interaction.response.send_message("**Impossible** • Cette couleur est utilisée par Discord pour les rôles sans couleur, utilisez plutôt #000001 pour du noir.", ephemeral=True)
        
        old_role = self.get_member_color_role(interaction.user)
        
        new_role = await self.fetch_color_role(interaction.guild, color, interaction.user)
        if not new_role:
            return await interaction.response.send_message("**Impossible** • La couleur donnée n'est pas valide.", ephemeral=True)

        if old_role and old_role != new_role: # Si son ancien rôle n'a pas été recyclé
            try:
                await interaction.user.remove_roles(old_role, reason="Changement de couleur")
            except discord.Forbidden:
                return await interaction.response.send_message("**Erreur** • Je n'ai pas la permission de retirer votre ancien rôle de couleur.", ephemeral=True)
            except discord.HTTPException:
                return await interaction.response.send_message("**Erreur** • Impossible de retirer votre ancien rôle de couleur.", ephemeral=True)
        
        try:
            await interaction.user.add_roles(new_role, reason=f"Rôle de couleur pour {interaction.user}")
        except discord.Forbidden:
            return await interaction.response.send_message("**Erreur** • Je n'ai pas la permission de vous donner ce rôle de couleur.", ephemeral=True)
        except discord.HTTPException:
            return await interaction.response.send_message("**Erreur** • Impossible de vous donner ce rôle de couleur.", ephemeral=True)
        
        text = f"Vous avez obtenu le rôle de couleur {new_role.mention}"
        await asyncio.sleep(0.1)
        if not self.is_color_displayed(interaction.user):
            text += "\n\n**Note :** Si vous ne voyez pas votre couleur, vérifiez que ce rôle est au-dessus de tous vos autres rôles colorés."
        em = discord.Embed(description=text, color=new_role.color)
        await interaction.response.send_message(embed=em, ephemeral=True)
        
    @mycolor_group.command(name='remove')
    async def remove_color_command(self, interaction: Interaction):
        """Retire les rôles de couleur gérés par le système"""
        if not isinstance(interaction.guild, discord.Guild) or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("Cette commande n'est pas disponible en dehors des serveurs.", ephemeral=True)
        
        await interaction.response.defer()
        roles = self.get_color_roles(interaction.guild)
        if not roles:
            return await interaction.followup.send("**Impossible** • Vous n'avez aucun rôle de couleur.", ephemeral=True)
        
        try:
            await interaction.user.remove_roles(*roles, reason="Retrait des rôles de couleur")
        except discord.Forbidden:
            return await interaction.followup.send("**Erreur** • Je n'ai pas la permission de retirer vos rôles de couleur.", ephemeral=True)
        except discord.HTTPException:
            return await interaction.followup.send("**Erreur** • Impossible de retirer vos rôles de couleur.", ephemeral=True)
        
        await self.clean_unused_color_roles(interaction.guild)
        await interaction.followup.send("**Succès** • Vos rôles de couleur ont été retirés.", ephemeral=True)
        
    @mycolor_group.command(name='avatar')
    @app_commands.rename(member='membre')
    async def preview_avatar_command(self, interaction: Interaction, member: discord.Member | None = None):
        """Choisir un rôle de couleur parmie les couleurs dominantes de votre avatar
        
        :param member: Autre membre dont l'avatar est utilisé
        """
        if not isinstance(interaction.guild, discord.Guild) or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("Cette commande n'est pas disponible en dehors des serveurs.", ephemeral=True)
        
        if not self.is_enabled(interaction.guild):
            return await interaction.response.send_message("**Non disponible** • Le système de rôles de couleur n'est pas activé sur ce serveur.", ephemeral=True)
        
        await interaction.response.defer(ephemeral=True)
        user = member or interaction.user
        previews = await self.draw_discord_emulation(user)
        if not previews:
            return await interaction.followup.send("**Impossible** • Aucune couleur n'a pu être extraite de votre avatar.", ephemeral=True)
        
        menu = AvatarPreviewSelectMenu(interaction, previews)
        await menu.start()
        await menu.wait()
        if not menu.result:
            return await interaction.followup.send("**Annulé** • Vous n'avez pas choisi de couleur.", ephemeral=True)
        
        color = menu.result.lstrip('#')
        old_role = self.get_member_color_role(interaction.user)
        new_role = await self.fetch_color_role(interaction.guild, color, interaction.user)
        if not new_role:
            return await interaction.followup.send("**Impossible** • La couleur donnée n'est pas valide.", ephemeral=True)
        
        if old_role and old_role != new_role: # Si son ancien rôle n'a pas été recyclé
            try:
                await interaction.user.remove_roles(old_role, reason="Changement de couleur")
            except discord.Forbidden:
                return await interaction.followup.send("**Erreur** • Je n'ai pas la permission de retirer votre ancien rôle de couleur.", ephemeral=True)
            except discord.HTTPException:
                return await interaction.followup.send("**Erreur** • Impossible de retirer votre ancien rôle de couleur.", ephemeral=True)
            
        try:
            await interaction.user.add_roles(new_role, reason=f"Rôle de couleur pour {interaction.user}")
        except discord.Forbidden:
            return await interaction.followup.send("**Erreur** • Je n'ai pas la permission de vous donner ce rôle de couleur.", ephemeral=True)
        except discord.HTTPException:
            return await interaction.followup.send("**Erreur** • Impossible de vous donner ce rôle de couleur.", ephemeral=True)
        
        text = f"Vous avez obtenu le rôle de couleur {new_role.mention}"
        if not self.is_color_displayed(interaction.user):
            text += "\n**Note :** Si vous ne voyez pas votre couleur, vérifiez que ce rôle est au-dessus de tous vos autres rôles colorés."
        em = discord.Embed(description=text, color=new_role.color)
        await interaction.followup.send(embed=em, ephemeral=True)
        
    config_group = app_commands.Group(name='config-colors', description="Configuration du système de rôles de couleur", guild_only=True, default_permissions=discord.Permissions(manage_roles=True))
    
    @config_group.command(name='enable')
    @app_commands.rename(enabled='activer')
    async def enable_color_command(self, interaction: Interaction, enabled: bool):
        """Activer ou désactiver le système de rôles de couleur sur le serveur
        
        :param enabled: True pour activer, False pour désactiver
        """
        if not isinstance(interaction.guild, discord.Guild):
            return await interaction.response.send_message("Cette commande n'est pas disponible en dehors des serveurs.", ephemeral=True)
        
        txt = f"**Paramètre modifié** • Le système de rôles de couleur a été **{'activé' if enabled else 'désactivé'}**."
        master_role = self.get_master_role(interaction.guild)
        if not master_role:
            txt += "\n**[!] Important :** Aucun rôle maître n'a été défini, ce rôle est essentiel pour que les rôles colorés soient bien situés dans la hiérarchie des rôles. Utilisez `/rcolors master` pour le définir."
        
        self.data.set_keyvalue_table_value(interaction.guild, 'settings', 'Enabled', int(enabled))
        await interaction.response.send_message(txt, ephemeral=True)
        
    @config_group.command(name='master')
    @app_commands.rename(role='rôle')
    async def master_color_command(self, interaction: Interaction, role: discord.Role | None = None):
        """Définir le rôle maître pour ordonner les rôles de couleur dans la liste des rôles
        
        :param role: Rôle maître servant de référence
        """
        if not isinstance(interaction.guild, discord.Guild):
            return await interaction.response.send_message("Cette commande n'est pas disponible en dehors des serveurs.", ephemeral=True)
        
        txt = f"**Paramètre modifié** • Le rôle maître a été défini sur {role.mention if role else 'aucun'}. Sachez que les rôles de couleurs seront arrangés en-dessous de ce rôle dans la liste des rôles."
        if role:
            if role.position >= interaction.guild.me.top_role.position:
                txt += "\n**[!] Important :** Le rôle maître doit être __en-dessous__ de mon rôle le plus haut pour que je puisse ordonner les rôles de couleur dans la liste des rôles."
        
        await interaction.response.defer()
        self.set_master_role(interaction.guild, role)
        await self.reorganize_color_roles(interaction.guild)
        await interaction.followup.send(txt, ephemeral=True)
        
    @config_group.command(name='reorganize')
    async def reorganize_color_command(self, interaction: Interaction):
        """Réorganiser manuellement les rôles de couleur du serveur sous le rôle maître"""
        if not isinstance(interaction.guild, discord.Guild):
            return await interaction.response.send_message("Cette commande n'est pas disponible en dehors des serveurs.", ephemeral=True)
        
        await interaction.response.defer()
        await self.reorganize_color_roles(interaction.guild)
        await interaction.followup.send("**Succès** • Les rôles de couleur ont été réorganisés.", ephemeral=True)
        
    @config_group.command(name='clean')
    async def clean_color_command(self, interaction: Interaction):
        """Supprimer les rôles de couleur inutilisés du serveur"""
        if not isinstance(interaction.guild, discord.Guild):
            return await interaction.response.send_message("Cette commande n'est pas disponible en dehors des serveurs.", ephemeral=True)
        
        await interaction.response.defer()
        nb = await self.clean_unused_color_roles(interaction.guild)
        await interaction.followup.send(f"**Succès** • {nb} rôles de couleur ont été supprimés.", ephemeral=True)

async def setup(bot):
    await bot.add_cog(Colors(bot))
