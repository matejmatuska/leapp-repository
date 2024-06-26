from leapp.actors import Actor
from leapp.libraries.actor import vsftpdconfigread
from leapp.models import DistributionSignedRPM, VsftpdFacts
from leapp.tags import FactsPhaseTag, IPUWorkflowTag


class VsftpdConfigRead(Actor):
    """
    Reads vsftpd configuration files (/etc/vsftpd/*.conf) and extracts necessary information.
    """

    name = 'vsftpd_config_read'
    consumes = (DistributionSignedRPM,)
    produces = (VsftpdFacts,)
    tags = (FactsPhaseTag, IPUWorkflowTag)

    def process(self):
        installed_rpm_facts = next(self.consume(DistributionSignedRPM))
        if vsftpdconfigread.is_processable(installed_rpm_facts):
            self.produce(vsftpdconfigread.get_vsftpd_facts())
