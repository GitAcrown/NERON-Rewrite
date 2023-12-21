import logging
import re
from datetime import datetime

import discord
from discord import Interaction, app_commands
from discord.ext import commands

from common import dataio
from common.utils import fuzzy
from common.utils.pretty import DEFAULT_EMBED_COLOR

logger = logging.getLogger(f'NERON.{__name__.capitalize()}')

DEFAULT_TRIGGERS = [
    {'label': 'twitter.com', 'search': 'https://twitter.com/', 'replace': 'https://vxtwitter.com/'},
    {'label': 'x.com', 'search': 'https://x.com/', 'replace': 'https://vxtwitter.com/'},
    {'label': 'threads.net', 'search': 'https://www.threads.net/', 'replace': 'https://www.vxthreads.net/'},
    {'label': 'vm.tiktok.com', 'search': 'https://vm.tiktok.com/', 'replace': 'https://vm.vxtiktok.com/'}
     ]

class CancelButtonView(discord.ui.View):
    """Ajoute un bouton permettant d'annuler la preview et restaurer celle du message original"""
    def __init__(self, link_message: discord.Message, replace_message: discord.Message, *, timeout: float | None = 7):
        super().__init__(timeout=timeout)
        self.link_msg = link_message
        self.replace_msg = replace_message
        self.cancelled = False

    @discord.ui.button(label='Annuler la correction', style=discord.ButtonStyle.red)
    async def cancel(self, interaction: Interaction, button: discord.ui.Button):
        self.cancelled = True
        await self.replace_msg.delete()
        await self.link_msg.edit(suppress=False)

    async def interaction_check(self, interaction: Interaction):
        if interaction.user != self.link_msg.author:
            await interaction.response.send_message('Seul l\'auteur du message peut annuler la prévisualisation.', ephemeral=True)
            return False
        return True
    
    async def on_timeout(self):
        if not self.cancelled:
            await self.replace_msg.edit(view=None)


class WebUtils(commands.Cog):
    """Outils autour des liens web postés sur Discord"""
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.data = dataio.get_cog_data(self)

        # Paramètres
        default_settings = {
            'EnableFixLinks': 1,
            'CancelFixButton': 1
        }
        self.data.register_keyvalue_table_for(discord.Guild, 'settings', default_values=default_settings)
        
        # Correcteurs de liens
        link_fixes = dataio.ObjectTableInitializer(
            table_name='link_fixes',
            create_query="""CREATE TABLE IF NOT EXISTS link_fixes (
                label TEXT PRIMARY KEY,
                search TEXT NOT NULL,
                replace TEXT NOT NULL
                )""",
            default_values=DEFAULT_TRIGGERS,
            fill_if_missing=False
        )
        self.data.register_tables_for(discord.Guild, [link_fixes])
        
        # TODO: Historique des liens postés
        
        self.__triggers_cache = {}
        
    def cog_unload(self):
        self.data.close_all()
    
    # Gestion des déclencheurs ---------------------------------------------------------------
    
    def get_triggers(self, guild: discord.Guild) -> list[dict]:
        """Renvoie la liste des déclencheurs pour le serveur"""
        r = self.data.get(guild).fetchall("SELECT * FROM link_fixes")
        return r if r else []
    
    def get_trigger(self, guild: discord.Guild, label: str) -> dict | None:
        """Renvoie un déclencheur pour le serveur"""
        r = self.data.get(guild).fetchone("SELECT * FROM link_fixes WHERE label = ?", (label,))
        return r
    
    def set_trigger(self, guild: discord.Guild, label: str, search: str, replace: str):
        """Ajoute un déclencheur pour le serveur"""
        self.data.get(guild).execute("INSERT OR REPLACE INTO link_fixes VALUES (?, ?, ?)", (label, search, replace))
    
    def delete_trigger(self, guild: discord.Guild, label: str):
        """Supprime un déclencheur pour le serveur"""
        self.data.get(guild).execute("DELETE FROM link_fixes WHERE label = ?", (label,))
    
    # Cache des déclencheurs ---------------------------------------------------------------
    
    def get_triggers_cache(self, guild: discord.Guild):
        """Renvoie la liste des déclencheurs pour le serveur"""
        if guild.id not in self.__triggers_cache:
            self.__triggers_cache[guild.id] = self.get_triggers(guild)
        return self.__triggers_cache[guild.id]
    
    def update_triggers_cache(self, guild: discord.Guild):
        """Met à jour la liste des déclencheurs pour le serveur"""
        self.__triggers_cache[guild.id] = self.get_triggers(guild)
        
    # Utils ---------------------------------------------------------------
    
    def get_label_for(self, base_url: str) -> str | None:
        """Détermine automatiquement un label pour une URL"""
        # On ne garde que la partie "nom de domaine"
        base_url = re.sub(r'^(https?://)?(www\.)?', '', base_url)
        base_url = re.sub(r'\/.*$', '', base_url)
        
        # On vérifie que le nom de domaine est valide
        if not re.match(r'^[a-zA-Z0-9\-\.]+$', base_url):
            return None
        return base_url.lower()
        
    # Events ---------------------------------------------------------------
    
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Gestion des déclencheurs regex"""
        if message.author.bot:
            return
        if not message.guild:
            return
        
        if not bool(self.data.get_keyvalue_table_value(message.guild, 'settings', 'EnableFixLinks')):
            return
        
        triggers = self.get_triggers_cache(message.guild)
        if not triggers:
            return
        
        links = re.findall(r'https?://[^\s]+', message.content)
        if not links:
            return
        
        links_content = '\n'.join(links)
        for trigger in triggers:
            links_content = re.sub(trigger['search'], trigger['replace'], links_content)
        
        if links_content != '\n'.join(links):
            replace_msg = await message.reply(links_content, mention_author=False)
            await message.edit(suppress=True)
            if bool(self.data.get_keyvalue_table_value(message.guild, 'settings', 'CancelFixButton', cast=int)):
                view = CancelButtonView(message, replace_msg)
                await replace_msg.edit(view=view)
        
    # Configuration ===============================================================
    
    fixlinks_group = app_commands.Group(name='fixlinks',
                                      description="Configuration de la correction de liens",
                                      guild_only=True,
                                      default_permissions=discord.Permissions(manage_messages=True))
    
    @fixlinks_group.command(name='enable')
    async def fixlinks_enable(self, interaction: Interaction, enable: bool):
        """Active ou désactive la correction de liens
        
        :param enable: True pour activer, False pour désactiver"""
        if not isinstance(interaction.guild, discord.Guild):
            return await interaction.response.send_message("Cette commande n'est pas disponible en MP", ephemeral=True)
        
        self.data.set_keyvalue_table_value(interaction.guild, 'settings', 'EnableLinkFix', int(enable))
        await interaction.response.send_message(f"**Correction de liens** • La correction de liens est maintenant **{'activée' if enable else 'désactivée'}**\nUtilisez `/fixlinks list` pour afficher les correcteurs configurés", ephemeral=True)
    
    @fixlinks_group.command(name='cancelbutton')
    async def fixlinks_cancelbutton(self, interaction: Interaction, enable: bool):
        """Active ou désactive l'affichage d'un bouton pour annuler la correction de liens
        
        :param enable: True pour activer, False pour désactiver"""
        if not isinstance(interaction.guild, discord.Guild):
            return await interaction.response.send_message("Cette commande n'est pas disponible en MP", ephemeral=True)
        
        self.data.set_keyvalue_table_value(interaction.guild, 'settings', 'CancelFixButton', int(enable))
        await interaction.response.send_message(f"**Bouton d'annulation** • Le bouton d'annulation est maintenant **{'activé' if enable else 'désactivé'}**", ephemeral=True)
    
    @fixlinks_group.command(name='list')
    async def fixlinks_list(self, interaction: Interaction):
        """Affiche la liste des déclencheurs utilisés pour corriger les liens"""
        if not isinstance(interaction.guild, discord.Guild):
            return await interaction.response.send_message("Cette commande n'est pas disponible en MP", ephemeral=True)
        
        triggers = self.get_triggers_cache(interaction.guild)
        if not triggers:
            return await interaction.response.send_message("**Vide** • Aucun correcteur n'a été configuré", ephemeral=True)
        
        embed = discord.Embed(title="Correcteurs de lien configurés", color=DEFAULT_EMBED_COLOR)
        embed.set_footer(text=f"Utilisez /fixlinks set <search> <replace> pour ajouter un correcteur")

        text = ""
        for trigger in triggers:
            text += f"- **{trigger['label']}** : `{trigger['search']}` → `{trigger['replace']}`\n"
        embed.description = text
        await interaction.response.send_message(embed=embed)
        
    @fixlinks_group.command(name='set')
    @app_commands.rename(replace='remplacement')
    async def fixlinks_set(self, interaction: Interaction, search: str, replace: str):
        """Ajouter ou modifier un déclencheur pour corriger les liens
        
        :param regex: Portion de lien à remplacer (ex: https://twitter.com/)
        :param replace: Remplacement à effectuer (ex: https://vxtwitter.com/)
        """
        if not isinstance(interaction.guild, discord.Guild):
            return await interaction.response.send_message("Cette commande n'est pas disponible en MP", ephemeral=True)
        
        triggers = self.get_triggers_cache(interaction.guild)
        if len(triggers) >= 20:
            return await interaction.response.send_message("**Erreur** • Vous avez atteint la limite de 20 correcteurs", ephemeral=True)
        
        label = self.get_label_for(search)
        if not label:
            return await interaction.response.send_message("**Erreur** • Le nom de domaine semble invalide", ephemeral=True)
        
        edit = self.get_trigger(interaction.guild, label) is not None
        
        self.set_trigger(interaction.guild, label, search, replace)
        self.update_triggers_cache(interaction.guild)
        await interaction.response.send_message(f"**Correcteur `{label}` {'modifié' if edit else 'ajouté'}** • `{search}` → `{replace}`", ephemeral=True)
        
    @fixlinks_group.command(name='remove')
    async def fixlinks_remove(self, interaction: Interaction, label: str):
        """Supprime un déclencheur pour corriger les liens
        
        :param label: Nom du déclencheur à supprimer
        """
        if not isinstance(interaction.guild, discord.Guild):
            return await interaction.response.send_message("Cette commande n'est pas disponible en MP", ephemeral=True)
        
        if not self.get_trigger(interaction.guild, label):
            return await interaction.response.send_message(f"**Erreur** • Le correcteur `{label}` n'existe pas", ephemeral=True)
        
        self.delete_trigger(interaction.guild, label)
        self.update_triggers_cache(interaction.guild)
        await interaction.response.send_message(f"**Correcteur `{label}` supprimé** • Ces liens ne seront plus détectés et corrigés", ephemeral=True)
        
    @fixlinks_remove.autocomplete('label')
    async def autocomplete_command(self, interaction: discord.Interaction, current: str):
        guild = interaction.guild
        if not guild:
            return []
        labels = [t['label'] for t in self.get_triggers_cache(guild)]
        r = fuzzy.finder(current, labels)
        return [app_commands.Choice(name=s, value=s) for s in r]

async def setup(bot):
    await bot.add_cog(WebUtils(bot))
