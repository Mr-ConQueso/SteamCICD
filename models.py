from peewee import *
import os
from pathlib import Path

db = SqliteDatabase('steamicicd.db')

class BaseModel(Model):
    class Meta:
        database = db

class Project(BaseModel):
    name = CharField()
    unity_org_id = CharField()
    unity_project_id = CharField()
    steam_app_id = CharField()
    steam_desc = CharField(default="Automatic CI/CD Build")
    steam_set_live = CharField(null=True)
    enabled = BooleanField(default=True)

class Depot(BaseModel):
    project = ForeignKeyField(Project, backref='depots')
    os = CharField() # Windows, Linux, MacOS, Android
    depot_id = CharField()

class GlobalSettings(BaseModel):
    key = CharField(unique=True)
    value = CharField()

def init_db():
    db.connect()
    db.create_tables([Project, Depot, GlobalSettings])
    
    # Migrate from env if database is empty
    if Project.select().count() == 0:
        unity_org_id = os.getenv("UNITY_ORG_ID")
        unity_project_id = os.getenv("UNITY_PROJECT_ID")
        steam_app_id = os.getenv("STEAM_APP_ID", "") # Might not be in env
        
        if unity_org_id and unity_project_id:
            project = Project.create(
                name="Default Project",
                unity_org_id=unity_org_id,
                unity_project_id=unity_project_id,
                steam_app_id=steam_app_id
            )
            print(f"Migrated default project: {project.name}")

    # Set default global settings from env if they don't exist
    for key in ["UNITY_API_KEY", "STEAMCMD_USERNAME", "STEAMCMD_PASSWORD"]:
        val = os.getenv(key)
        if val:
            GlobalSettings.get_or_create(key=key, defaults={'value': val})

if __name__ == "__main__":
    init_db()
