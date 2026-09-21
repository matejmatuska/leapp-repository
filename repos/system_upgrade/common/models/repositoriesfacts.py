from leapp.models import fields, Model
from leapp.topics import SystemFactsTopic


class RepositoryData(Model):
    topic = SystemFactsTopic

    repoid = fields.String()
    name = fields.String()
    baseurl = fields.Nullable(fields.String())
    metalink = fields.Nullable(fields.String())
    mirrorlist = fields.Nullable(fields.String())
    enabled = fields.Boolean(default=True)
    additional_fields = fields.Nullable(fields.String())
    proxy = fields.Nullable(fields.String())


class RepositoryFile(Model):
    topic = SystemFactsTopic

    file = fields.String()
    data = fields.List(fields.Model(RepositoryData))


class RepositoriesFacts(Model):
    topic = SystemFactsTopic

    repositories = fields.List(fields.Model(RepositoryFile))


class RepositoriesFactsTarget(Model):
    """
    Point-in-time snapshot of the repofiles present in the target userspace
    container, captured immediately after its creation.

    The data is read from the repofiles inside the container, which in the RHUI
    case may differ from the repositories bundled with the target OS.

    The snapshot is retained for auditing/post-mortem purposes and it MAY not
    reflect later modifications of the repofiles (e.g. done by the
    adjustlocalrepos actor). Consumers requiring the current state of the
    repofiles have to read them from the TargetUserSpaceInfo.path directory.
    """
    topic = SystemFactsTopic

    repositories = fields.List(fields.Model(RepositoryFile))
