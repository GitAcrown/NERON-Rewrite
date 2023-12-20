# Script de gestion centralisée des données liées aux modules (réécriture v3)

import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable, Sequence, Type

import discord
from discord.ext import commands

__COGDATA_INSTANCES : dict[str, 'CogData'] = {}

class CogData:
    """Classe de gestion des données liées à un module
    
    Cette classe ne doit pas être instanciée directement, mais récupérée via la fonction get_cog_data()"""
    def __init__(self, cog_name: str):
        self.cog_name = cog_name
        self.cog_folder = Path(f'cogs/{cog_name}')
        
        self.__connections : dict[discord.abc.Snowflake | str, sqlite3.Connection] = {}
        self.__initializers : dict[Type[discord.abc.Snowflake] | str, list[ObjectTableInitializer]] = {}
        
    def __repr__(self) -> str:
        return f'<CogData {self.cog_name}>'
    
    # ---- Dossiers ----
    
    def get_folder(self, folder_name: str, *, create: bool = False) -> Path:
        """Récupérer un dossier lié au module
        
        :param folder_name: Nom du dossier
        :param create: Si True, le dossier est créé s'il n'existe pas (False par défaut)"""
        folder = self.cog_folder / folder_name
        if create and not folder.exists():
            folder.mkdir()
        return folder
    
    # ---- Bases de données ----
    
    def __get_sqlite_connection(self, obj: discord.abc.Snowflake | str) -> sqlite3.Connection:
        db_name = _get_object_db_name(obj)
        folder = self.cog_folder / 'data'
        if not folder.exists():
            folder.mkdir()
        conn = sqlite3.connect(folder / f'{db_name}.db')
        conn.row_factory = sqlite3.Row # On veut récupérer les résultats sous forme de dictionnaires
        return conn
    
    def __initialize_tables(self, obj: discord.abc.Snowflake | str) -> sqlite3.Connection:
        """Initialise les tables de données d'un objet par rapport aux initialisateurs enregistrés pour son type"""
        obj_type = type(obj) if isinstance(obj, discord.abc.Snowflake) else obj
        conn = self.__get_sqlite_connection(obj)
        with closing(conn.cursor()) as cursor:
            for i in self.get_initializers_for(obj_type):
                cursor.execute(i.create_query)
                if i.default_values:
                    cursor.executemany(f'INSERT OR IGNORE INTO {i.table_name} ({",".join(i.default_values[0].keys())}) VALUES ({",".join(["?"] * len(i.default_values[0]))})', 
                                       [tuple(row.values()) for row in i.default_values])
        conn.commit()
        return conn
            
    # ---- Gestion des tables ----
    
    def get(self, obj: discord.abc.Snowflake | str) -> 'ObjectData':
        """Récupère les données liées à un objet

        :param obj: L'objet dont on veut récupérer les données
        :return: Instance de ObjectData
        """
        if obj not in self.__connections:
            self.__connections[obj] = self.__initialize_tables(obj)
        return ObjectData(obj, self.__connections[obj])
    
    def get_all(self, obj_type: Type[discord.abc.Snowflake] | None = None) -> list['ObjectData']:
        """Récupère les données liées à tous les objets chargés dans le cache CogData

        :param obj_type: Type d'objet à récupérer (discord.abc.Snowflake)
        :return: Liste d'instances de ObjectData
        """
        if obj_type is None:
            return [ObjectData(obj, conn) for obj, conn in self.__connections.items()]
        else:
            return [ObjectData(obj, conn) for obj, conn in self.__connections.items() if isinstance(obj, obj_type)]
    
    def close(self, obj: discord.abc.Snowflake | str) -> None:
        """Ferme la connexion à la base de données d'un objet

        :param obj: L'objet dont on veut fermer la connexion
        """
        if obj in self.__connections:
            self.__connections[obj].close()
            del self.__connections[obj]
            
    def close_all(self) -> None:
        """Ferme toutes les connexions ouvertes à la base de données"""
        for obj in self.__connections:
            self.__connections[obj].close()
        self.__connections.clear()
    
    def delete(self, obj: discord.abc.Snowflake | str) -> None:
        """Supprime les données liées à un objet

        :param obj: L'objet dont on veut supprimer les données
        """
        db_name = _get_object_db_name(obj)
        folder = self.cog_folder / 'data'
        if not folder.exists():
            return
        db_file = folder / f'{db_name}.db'
        if db_file.exists():
            db_file.unlink()
            
    def delete_all(self) -> None:
        """Supprime toutes les données liées aux objets"""
        folder = self.cog_folder / 'data'
        if not folder.exists():
            return
        for db_file in folder.iterdir():
            db_file.unlink()
            
    # ---- Initialisation des tables ----
    
    def register_tables_for(self, obj_type: Type[discord.abc.Snowflake] | str, initializers: Iterable['ObjectTableInitializer']) -> None:
        """Enregistre des initialisateurs de tables de données pour un type d'objet

        :param obj_type: Type d'objet concerné par les initialisateurs
        :param initializers: Liste d'initialisateurs
        """
        if obj_type not in self.__initializers:
            self.__initializers[obj_type] = []
        for i in initializers:
            if not isinstance(i, ObjectTableInitializer):
                raise TypeError(f'Expected ObjectTableInitializer, got {type(i)}')
            if i not in self.__initializers[obj_type]:
                self.__initializers[obj_type].append(i)
    
    def get_initializers_for(self, obj_type: Type[discord.abc.Snowflake] | str) -> list['ObjectTableInitializer']:
        """Récupère les initialisateurs pour un type d'objet

        :param obj_type: Type d'objet concerné par les initialisateurs
        :return: Liste d'initialisateurs
        """
        return self.__initializers.get(obj_type, [])
    
    # ---- Tables de type clé-valeur ----
    
    def register_keyvalue_table_for(self, obj_type: Type[discord.abc.Snowflake] | str, table_name: str, *, default_values: dict[str, Any] = {}) -> None:
        """Enregistre un initialisateur pour une table clé-valeur simple

        :param obj_type: Type d'objet concerné par l'initialisateur
        :param table_name: Nom de la table
        :param default_values: Valeurs par défaut à insérer dans la table
        """
        table_name = re.sub(r'[^a-z0-9_]', '_', table_name.lower())
        create_query = f'CREATE TABLE IF NOT EXISTS {table_name} (key TEXT PRIMARY KEY, value TEXT)'
        insert_values = [{'key': str(k), 'value': str(v)} for k, v in default_values.items()]
        self.register_tables_for(obj_type, [ObjectTableInitializer(table_name, create_query, default_values=insert_values)])
        
    def get_keyvalue_table_values(self, obj: discord.abc.Snowflake | str, table_name: str) -> dict[str, str]:
        """Récupère l'intégralité des valeurs d'une table clé-valeur
        
        :param obj: L'objet dont on veut récupérer les données
        :param table_name: Nom de la table
        :return: Dictionnaire des valeurs
        """
        try:
            _get_object_db_name(obj)
        except TypeError:
            raise TypeError(f'Type d\'objet invalide : {type(obj)}')
        data = self.get(obj)
        if table_name not in data.tables:
            raise ValueError(f'La table {table_name} n\'existe pas')
        r = data.fetchall(f'SELECT * FROM {table_name}')
        return {row['key']: row['value'] for row in r}
    
    def get_keyvalue_table_value(self, obj: discord.abc.Snowflake | str, table_name: str, key: str, *, cast: Type[Any] = str) -> Any:
        """Récupère une valeur d'une table clé-valeur
        
        :param obj: L'objet dont on veut récupérer les données
        :param table_name: Nom de la table
        :param key: Clé de la valeur
        :param cast: Type de la valeur
        :return: Valeur
        """
        try:
            _get_object_db_name(obj)
        except TypeError:
            raise TypeError(f'Type d\'objet invalide : {type(obj)}')
        data = self.get(obj)
        if table_name not in data.tables:
            raise ValueError(f'La table {table_name} n\'existe pas')
        r = data.fetchone(f'SELECT * FROM {table_name} WHERE key = ?', (key,))
        if r is None:
            raise ValueError(f'La clé {key} n\'existe pas')
        return cast(r['value'])
    
    def set_keyvalue_table_value(self, obj: discord.abc.Snowflake | str, table_name: str, key: str, value: Any) -> None:
        """Définit une valeur d'une table clé-valeur
        
        :param obj: L'objet dont on veut récupérer les données
        :param table_name: Nom de la table
        :param key: Clé de la valeur
        :param value: Valeur
        """
        try:
            _get_object_db_name(obj)
        except TypeError:
            raise TypeError(f'Type d\'objet invalide : {type(obj)}')
        data = self.get(obj)
        if table_name not in data.tables:
            raise ValueError(f'La table {table_name} n\'existe pas')
        try:
            value = str(value)
        except:
            raise TypeError('Impossible de convertir la valeur en str')
        data.execute(f'INSERT OR REPLACE INTO {table_name} (key, value) VALUES (?, ?)', (key, value))
        
    def delete_keyvalue_table_value(self, obj: discord.abc.Snowflake | str, table_name: str, key: str) -> None:
        """Supprime une valeur d'une table clé-valeur
        
        :param obj: L'objet dont on veut récupérer les données
        :param table_name: Nom de la table
        :param key: Clé de la valeur
        """
        try:
            _get_object_db_name(obj)
        except TypeError:
            raise TypeError(f'Type d\'objet invalide : {type(obj)}')
        data = self.get(obj)
        if table_name not in data.tables:
            raise ValueError(f'La table {table_name} n\'existe pas')
        data.execute(f'DELETE FROM {table_name} WHERE key = ?', (key,))


class ObjectData:
    """Classe de gestion des données liées à un objet Discord (discord.Guild, discord.Member, etc.) ou à un nom libre
    
    Cette classe ne doit pas être instanciée directement, mais récupérée via la méthode get() de CogData"""
    def __init__(self, obj: discord.abc.Snowflake | str, conn: sqlite3.Connection):
        self.obj = obj
        self.conn = conn
        
    def __repr__(self) -> str:
        return f'<ObjectData {self.obj}>'
    
    # ---- Propriétés ----

    @property
    def tables(self) -> list[str]:
        r = self.fetchall("SELECT name FROM sqlite_master WHERE type='table'")
        return [row['name'] for row in r]
    
    # ---- Gestion des données ----
    
    def execute(self, query: str, *args, commit: bool = True) -> None:
        """Exécute une requête SQL sans retour de données

        :param query: Requête SQL
        :param commit: Si True, la transaction est commitée après l'exécution de la requête (True par défaut)
        """
        with closing(self.conn.cursor()) as cursor:
            cursor.execute(query, *args)
        if commit:
            self.conn.commit()
            
    def executemany(self, query: str, *args, commit: bool = True) -> None:
        """Exécute plusieurs requêtes SQL sans retour de données

        :param query: Requête SQL
        :param commit: Si True, la transaction est commitée après l'exécution de la requête (True par défaut)
        """
        with closing(self.conn.cursor()) as cursor:
            cursor.executemany(query, *args)
        if commit:
            self.conn.commit()
            
    def fetchone(self, query: str, *args) -> dict[str, Any] | None:
        """Retourne la première ligne de résultat d'une requête SQL

        :param query: Requête SQL
        :return: Dictionnaire des données de la première ligne de résultat
        """
        with closing(self.conn.cursor()) as cursor:
            cursor.execute(query, *args)
            return cursor.fetchone()
        
    def fetchall(self, query: str, *args) -> list[dict[str, Any]]:
        """Retourne toutes les lignes de résultat d'une requête SQL

        :param query: Requête SQL
        :return: Liste de dictionnaires des données de chaque ligne de résultat
        """
        with closing(self.conn.cursor()) as cursor:
            cursor.execute(query, *args)
            return cursor.fetchall()
        
    def commit(self) -> None:
        """Commit la transaction en cours manuellement"""
        self.conn.commit()
        
    def rollback(self) -> None:
        """Effectue un rollback de la transaction en cours manuellement"""
        self.conn.rollback()


class ObjectTableInitializer:
    def __init__(self, 
                 table_name: str,
                 create_query: str,
                 *,
                 default_values: Sequence[dict[str, Any]] = []):
        """Initialiseur de table de données

        :param table_name: Nom de la table à créer
        :param create_query: Requête SQL de création de table
        :param default_values: Valeurs par défaut à insérer dans la table
        """
        self.table_name = table_name
        
        if not create_query.lower().startswith('create table'):
            raise ValueError('La valeur create_query doit être une requête SQL de création de table (CREATE TABLE ...)')
        self.create_query = create_query
        
        if default_values:
            # On vérifie que les clés sont les mêmes pour toutes les lignes
            keys = set(default_values[0].keys())
            if not all(set(row.keys()) == keys for row in default_values):
                raise ValueError('Toutes les lignes de default_values doivent avoir les mêmes clés')
        self.default_values = default_values
        
    def __repr__(self) -> str:
        return f'<ObjectTableInitializer {self.table_name}>'
    
    # ---- Propriétés ----
    
    @property
    def columns(self) -> list[str]:
        """Liste des colonnes de la table"""
        return re.findall(r'(?<=\()[^)]+(?=\))', self.create_query)[0].split(',')
    
    @property
    def is_keyvalue(self) -> bool:
        """Retourne True si la table est de type clé-valeur"""
        return len(self.columns) == 2 and self.columns[0].strip().lower() == 'key' and self.columns[1].strip().lower() == 'value'
    
# ========= DATAIO =========

def get_cog_data(cog: commands.Cog | str) -> CogData:
    """Renvoie une instance de CogData pour un module donné
    
    :param cog: Module ou nom du module"""
    cog_name = cog.lower() if isinstance(cog, str) else cog.qualified_name.lower()
    if cog_name not in __COGDATA_INSTANCES:
        __COGDATA_INSTANCES[cog_name] = CogData(cog_name)
    return __COGDATA_INSTANCES[cog_name]

def _get_object_db_name(obj: discord.abc.Snowflake | str) -> str:
    if isinstance(obj, discord.abc.Snowflake):
        return f'{obj.__class__.__name__}_{obj.id}'.lower()
    elif isinstance(obj, str):
        return re.sub(r'[^a-z0-9_]', '_', obj.lower())
    else:
        raise TypeError(f'Expected discord.abc.Snowflake or str, got {type(obj)}')
    