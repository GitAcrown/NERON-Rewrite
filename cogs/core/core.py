# Ce module est essentiel dans le fonctionnement du bot et ne doit pas être supprimé

import io
import logging
import textwrap
import traceback
from contextlib import redirect_stdout
from datetime import datetime
from typing import Any, Optional

import discord
import pytz
from discord import app_commands
from discord.ext import commands

from common import dataio
from common.utils import fuzzy, pretty

logger = logging.getLogger(f'NERON.{__name__.split(".")[-1]}')

class HelpMenuView(discord.ui.View):
    """Menu d'aide des commandes"""
    def __init__(self, cog: 'Core', original_interaction: discord.Interaction, *, start_at: str | None, timeout: float | None = 120):
        super().__init__(timeout=timeout)
        self.__cog = cog
        self.__interaction = original_interaction
        self.__start_at = start_at
        
        self.commands : dict[str, list[app_commands.Command | app_commands.Group]] = cog._get_bot_commands()
        self.ctx_commands : dict[str, list[app_commands.Command]] = cog._get_ctx_commands()
        self.pages = self.__build_pages()
        self.current_page = 0
        self.message: discord.Message | None = None
        
    def __build_pages(self) -> list[discord.Embed]:
        pages = []
        self.commands = {k: v for k, v in sorted(self.commands.items(), key=lambda c: c[0])}
        for cog_name, commands in self.commands.items():
            commands = sorted(commands, key=lambda c: c.qualified_name)
            cog = self.__cog.bot.get_cog(cog_name)
            if not cog:
                continue
            embed = discord.Embed(title=f"Aide pour les commandes • `{cog.qualified_name}`", color=0x2b2d31)
            text = f"*{cog.description}*\n_ _\n"
            for command in commands:
                if isinstance(command, app_commands.Group):
                    chunk = ''
                    for subcommand in command.commands:
                        if subcommand.qualified_name == self.__start_at:
                            chunk += f"- **`/{subcommand.qualified_name}` - {subcommand.description}**\n"
                        else:
                            chunk += f"- `/{subcommand.qualified_name}` - {subcommand.description}\n"
                    embed.add_field(name=command.qualified_name, value=chunk, inline=False)
                elif isinstance(command, app_commands.ContextMenu):
                    if command.qualified_name == self.__start_at:
                        text += f"- **`Applications > {command.qualified_name}` - {command.description}**\n"
                    else:
                        text += f"- `Applications > {command.qualified_name}` - {command.description}\n"
                else:
                    if command.qualified_name == self.__start_at:
                        text += f"- **`/{command.qualified_name}` - {command.description}**\n"
                    else:
                        text += f"- `/{command.qualified_name}` - {command.description}\n"
            embed.description = text
            if self.ctx_commands:
                embed.set_footer(text=f"Page {len(pages) + 1}/{len(self.commands) + 1} • Testez les commandes pour plus d'infos sur les arguments")
            else:
                embed.set_footer(text=f"Page {len(pages) + 1}/{len(self.commands)} • Testez les commandes pour plus d'infos sur les arguments")
            pages.append(embed)
        
        if self.ctx_commands:
            # On crée une page supplémentaire pour les commandes contextuelles
            embed = discord.Embed(title=f"Aide pour les commandes contextuelles", color=0x2b2d31)
            text = f"*Les commandes contextuelles sont des commandes qui s'activent en faisant un clic droit sur un message ou un utilisateur.*\n_ _\n"
            for type, commands in self.ctx_commands.items():
                commands = sorted(commands, key=lambda c: c.qualified_name)
                chunk = ''
                for command in commands:
                    desc = command.extras['description'] if 'description' in command.extras else 'Aucune description'
                    if command.qualified_name == self.__start_at:
                        chunk += f"- **`{command.qualified_name}` - {desc}**\n"
                    else:
                        chunk += f"- `{command.qualified_name}` - {desc}\n"
                if chunk:
                    embed.add_field(name=type, value=chunk, inline=False)
            embed.description = text
            embed.set_footer(text=f"Commandes contextuelles • Utilisez une flèche pour revenir aux commandes classiques")
            pages.append(embed)
        return pages
            
    async def start(self):
        """Démarre le menu d'aide"""
        if self.__start_at:
            # Si c'est une commande classique, on cherche la page correspondante
            for command in self.commands.values():
                for c in command:
                    if isinstance(c, app_commands.Group):
                        for subcommand in c.commands:
                            if subcommand.qualified_name == self.__start_at:
                                self.current_page = list(self.commands.values()).index(command)
                                break
                    elif c.qualified_name == self.__start_at:
                        self.current_page = list(self.commands.values()).index(command)
                        break
            # Si c'est une commande contextuelle, on va à la dernière page
            for type, commands in self.ctx_commands.items():
                for command in commands:
                    if command.qualified_name == self.__start_at:
                        self.current_page = len(self.commands)
                        break
                    
        embed = self.pages[self.current_page]
        self.message = await self.__interaction.followup.send(embed=embed, view=self)
            
    @discord.ui.button(style=discord.ButtonStyle.blurple, emoji=pretty.DEFAULT_ICONS_EMOJIS['back'])
    async def previous_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Page précédente"""
        self.current_page = self.current_page - 1 if self.current_page > 0 else len(self.pages) - 1
        await interaction.response.edit_message(embed=self.pages[self.current_page], view=self)
        
    @discord.ui.button(style=discord.ButtonStyle.red, emoji=pretty.DEFAULT_ICONS_EMOJIS['close'])
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Ferme le menu"""
        self.clear_items()
        if self.message:
            await self.message.delete()
        self.stop()
        
    @discord.ui.button(style=discord.ButtonStyle.blurple, emoji=pretty.DEFAULT_ICONS_EMOJIS['next'])
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Page suivante"""
        self.current_page = self.current_page + 1 if self.current_page < len(self.pages) - 1 else 0
        await interaction.response.edit_message(embed=self.pages[self.current_page], view=self)
    
    async def on_timeout(self):
        """Appelé lorsque le menu expire"""
        self.clear_items()
        if self.message:
            await self.message.delete()
        self.stop()

class Core(commands.Cog):
    """Module central du bot, contenant des commandes de base."""
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.data = dataio.get_cog_data(self)
        
        # Préférences globales par défaut des serveurs
        default_preferences = {
            'MainTextChannelID': 0,
            'Timezone': 'Europe/Paris'
        }
        self.data.register_keyvalue_table_for(discord.Guild, 'global_settings', default_values=default_preferences)
        
        self.timezones = pytz.all_timezones
        
        self._last_result: Optional[Any] = None

    # Gestion des commandes et modules ------------------------------

    @commands.command(name="load", hidden=True)
    @commands.is_owner()
    async def load(self, ctx, *, cog: str):
        """Charge un module"""
        cog_path = f'cogs.{cog}.{cog}'
        try:
            await self.bot.load_extension(cog_path)
        except Exception as exc:
            await ctx.send(f"**`ERREUR :`** {type(exc).__name__} - {exc}")
        else:
            await ctx.send("**`SUCCÈS`**")

    @commands.command(name="unload", hidden=True)
    @commands.is_owner()
    async def unload(self, ctx, *, cog: str):
        """Décharge un module"""
        cog_path = f'cogs.{cog}.{cog}'
        try:
            await self.bot.unload_extension(cog_path)
        except Exception as exc:
            await ctx.send(f"**`ERREUR :`** {type(exc).__name__} - {exc}")
        else:
            await ctx.send("**`SUCCÈS`**")

    @commands.command(name="reload", hidden=True)
    @commands.is_owner()
    async def reload(self, ctx, *, cog: str):
        """Recharge un module"""
        cog_path = f'cogs.{cog}.{cog}'
        try:
            await self.bot.reload_extension(cog_path)
        except Exception as exc:
            await ctx.send(f"**`ERREUR :`** {type(exc).__name__} - {exc}")
        else:
            await ctx.send("**`SUCCÈS`**")
            
    @commands.command(name="reloadall", hidden=True)
    @commands.is_owner()
    async def reloadall(self, ctx):
        """Recharge tous les modules"""
        for ext_name, _ext in self.bot.extensions.items():
            try:
                await self.bot.reload_extension(ext_name)
            except Exception as exc:
                await ctx.send(f"**`ERREUR :`** {type(exc).__name__} - {exc}")
        await ctx.send("**`SUCCÈS`**")

    @commands.command(name="extensions", hidden=True)
    @commands.is_owner()
    async def extensions(self, ctx):
        for ext_name, _ext in self.bot.extensions.items():
            await ctx.send(ext_name)

    @commands.command(name="cogs", hidden=True)
    @commands.is_owner()
    async def cogs(self, ctx):
        for cog_name, _cog in self.bot.cogs.items():
            await ctx.send(cog_name)
            
    # Commandes d'évaluation de code ------------------------------
            
    def cleanup_code(self, content: str) -> str:
        """Automatically removes code blocks from the code."""
        # remove ```py\n```
        if content.startswith('```') and content.endswith('```'):
            return '\n'.join(content.split('\n')[1:-1])

        # remove `foo`
        return content.strip('` \n')
            
    @commands.command(name='eval', hidden=True)
    @commands.is_owner()
    async def eval_code(self, ctx: commands.Context, *, body: str):
        """Evalue du code"""

        env = {
            'bot': self.bot,
            'ctx': ctx,
            'channel': ctx.channel,
            'author': ctx.author,
            'guild': ctx.guild,
            'message': ctx.message,
            '_': self._last_result,
        }

        env.update(globals())

        body = self.cleanup_code(body)
        stdout = io.StringIO()

        to_compile = f'async def func():\n{textwrap.indent(body, "  ")}'

        try:
            exec(to_compile, env)
        except Exception as e:
            return await ctx.send(f'```py\n{e.__class__.__name__}: {e}\n```')

        func = env['func']
        try:
            with redirect_stdout(stdout):
                ret = await func()
        except Exception as e:
            value = stdout.getvalue()
            await ctx.send(f'```py\n{value}{traceback.format_exc()}\n```')
        else:
            value = stdout.getvalue()
            try:
                await ctx.message.add_reaction('\u2705')
            except:
                pass

            if ret is None:
                if value:
                    await ctx.send(f'```py\n{value}\n```')
            else:
                self._last_result = ret
                await ctx.send(f'```py\n{value}{ret}\n```')
                
    @app_commands.command(name="ping")
    async def ping(self, interaction: discord.Interaction) -> None:
        """Renvoie le ping du bot"""
        await interaction.response.send_message(f"Pong ! (`{round(self.bot.latency * 1000)}ms`)")
        
    @app_commands.command(name="guildtime")
    async def guildtime(self, interaction: discord.Interaction) -> None:
        """Renvoie l'heure locale basée sur le fuseau horaire défini sur le serveur"""
        if not isinstance(interaction.guild, discord.Guild):
            await interaction.response.send_message("**Erreur** • Cette commande ne peut être utilisée que sur un serveur.", ephemeral=True)
            return
        tz = self.get_guild_global_setting(interaction.guild, 'Timezone')
        await interaction.response.send_message(f"**Date locale** • {datetime.now(pytz.timezone(tz)).strftime('%d/%m/%Y %H:%M:%S')} ({tz})")
                
    # Commandes d'aide des commandes ------------------------------
    
    def _get_bot_commands(self):
        cogs = self.bot.cogs
        modules = {}
        for cog_name, cog in cogs.items():
            modules[cog_name] = []
            for command in cog.get_app_commands():
                modules[cog_name].append(command)
        return modules
    
    def _get_ctx_commands(self):
        types = {'Utilisateur > Applications': [], 'Message > Applications': []}
        for user_commands in self.bot.tree.get_commands(type=discord.AppCommandType.user):
            types['Utilisateur > Applications'].append(user_commands)
        for message_commands in self.bot.tree.get_commands(type=discord.AppCommandType.message):
            types['Message > Applications'].append(message_commands)
        return types
    
    @app_commands.command(name="help")
    @app_commands.rename(command='commande')
    async def help(self, interaction: discord.Interaction, command: str | None):
        """Affiche l'aide des commandes du bot
        
        :param command: Nom d'une commande spécifique à pointer
        """
        await interaction.response.defer()
        view = HelpMenuView(self, interaction, start_at=command)
        await view.start()
        
    @help.autocomplete('command')
    async def autocomplete_command(self, interaction: discord.Interaction, current: str):
        lcoms = self.bot.tree.get_commands()
        all_commands = []
        for command in lcoms:
            if isinstance(command, app_commands.Group):
                all_commands.extend(command.commands)
            else:
                all_commands.append(command)
        r = fuzzy.finder(current, all_commands, key=lambda c: c.qualified_name)
        return [app_commands.Choice(name=c.qualified_name, value=c.qualified_name) for c in r][:5]
    
    # Commandes de gestion des préférences ------------------------------
    
    def get_guild_global_settings(self, guild: discord.Guild) -> dict[str, str]:
        """Récupère les valeurs brutes des préférences globales

        :param guild: Serveur concerné
        :return: Dictionnaire de strings des préférences globales
        """
        return self.data.get_keyvalue_table_values(guild, 'global_settings')
    
    def get_guild_global_setting(self, guild: discord.Guild, key: str, *, cast: type = str) -> Any:
        """Récupère une valeur d'une préférence globale

        :param guild: Serveur concerné
        :param key: Clé de la préférence
        :param cast: Type de la valeur à récupérer, par défaut str
        :return: Valeur de la préférence
        """
        return self.data.get_keyvalue_table_value(guild, 'global_settings', key, cast=cast)
    
    def set_guild_global_setting(self, guild: discord.Guild, key: str, value: Any) -> None:
        """Définit la valeur d'une préférence globale
        
        :param guild: Serveur concerné
        :param key: Clé de la préférence
        :param value: Valeur à définir (doit pouvoir être convertie en str)
        """
        self.data.set_keyvalue_table_value(guild, 'global_settings', key, value)
        
    config_group = app_commands.Group(name='config', description="Paramètres généraux du bot sur ce serveur", guild_only=True, default_permissions=discord.Permissions(manage_guild=True))
    
    @config_group.command(name="timezone")
    async def cmd_config_timezone(self, interaction: discord.Interaction, timezone: str | None = None):
        """Définir le fuseau horaire utilisé pour les commandes sur ce serveur
        
        :param timezone: Fuseau horaire (ex. Europe/Paris)
        """
        if not isinstance(interaction.guild, discord.Guild):
            await interaction.response.send_message("**Erreur** • Cette commande ne peut être utilisée que sur un serveur.", ephemeral=True)
            return
        
        if not timezone:
            current_timezone = self.get_guild_global_setting(interaction.guild, 'Timezone')
            await interaction.response.send_message(f"**Fuseau horaire actuel** • `{current_timezone}`", ephemeral=True)
            return
        
        if timezone not in self.timezones:
            await interaction.response.send_message(f"**Erreur** • Le fuseau horaire `{timezone}` n'existe pas. Consultez https://en.wikipedia.org/wiki/List_of_tz_database_time_zones pour la liste des fuseaux horaires disponibles.", ephemeral=True)
            return
        
        self.set_guild_global_setting(interaction.guild, 'Timezone', timezone)
        await interaction.response.send_message(f"**Succès** • Le fuseau horaire du serveur a été défini sur `{timezone}`.", ephemeral=True)
        
    @cmd_config_timezone.autocomplete('timezone')
    async def autocomplete_timezone(self, interaction: discord.Interaction, current: str):
        r = fuzzy.finder(current, self.timezones)
        return [app_commands.Choice(name=t, value=t) for t in r][:10]
    
    @config_group.command(name="mainchannel")
    async def cmd_config_mainchannel(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        """Définir le salon écrit principal du serveur
        
        :param channel: Salon principal
        """
        if not isinstance(interaction.guild, discord.Guild):
            await interaction.response.send_message("**Erreur** • Cette commande ne peut être utilisée que sur un serveur.", ephemeral=True)
            return
        
        if not channel:
            current_channel_id = self.get_guild_global_setting(interaction.guild, 'MainTextChannelID', cast=int)
            current_channel = interaction.guild.get_channel(current_channel_id)
            if not current_channel:
                await interaction.response.send_message(f"**Salon principal actuel** • Aucun salon défini", ephemeral=True)
                return
            await interaction.response.send_message(f"**Salon principal actuel** • {current_channel.mention}", ephemeral=True)
            return
        
        self.set_guild_global_setting(interaction.guild, 'MainTextChannelID', channel.id)
        await interaction.response.send_message(f"**Succès** • Le salon principal du serveur a été défini sur {channel.mention}.", ephemeral=True)

async def setup(bot):
    await bot.add_cog(Core(bot))
