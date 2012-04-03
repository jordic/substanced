import colander

from substanced.folder import Folder
from substanced.content import content
from substanced.sdi import Schema

from .interfaces import ISite

class SiteSchema(Schema):
    name = colander.SchemaNode(colander.String())
    description = colander.SchemaNode(colander.String())

@content(ISite)
class Site(Folder):
    __schema__ = SiteSchema()
