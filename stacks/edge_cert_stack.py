"""Edge ACM certificate CDK stack for PharmaPulse (task 18.1).

Provisions the TLS certificate that CloudFront requires for the custom
domain ``demo.awsteach.com``. CloudFront only accepts ACM certificates
issued in **us-east-1** regardless of where the distribution's origin
lives, so this stack is pinned to the Edge_Region (us-east-1) while the
rest of the system runs in the primary region (us-east-2). This is the one
standard relaxed for edge resources — the region-placement rule
(Requirement 17.6) — while least-privilege, encryption-at-rest, and tagging
standards still apply (Requirement 17.7). The certificate itself satisfies
Requirement 14.4 (ACM certificate provisioned in the Edge_Region for the
CloudFront distribution).

The certificate is DNS-validated against the public Route 53 hosted zone
``awsteach.com`` (Requirement 14.5's hosted zone), so validation records are
created automatically and the deploy is non-interactive (Requirement 20.1).

===========================================================================
app.py WIRING (do NOT edit app.py here — task 13 is editing it concurrently)
===========================================================================
This stack is designed to be wired into ``app.py`` with the edge environment
(us-east-1) and the foundation permissions boundary. Add the following after
the existing stacks are created (and after ``foundation`` exists):

    from stacks.edge_cert_stack import EdgeCertStack
    from stacks.config import HOSTED_ZONE_NAME, DEMO_DOMAIN

    cert = EdgeCertStack(
        app,
        "PharmaPulse-EdgeCert",
        env=edge_env,  # us-east-1 (Requirement 14.4 / 17.6)
        hosted_zone_name=HOSTED_ZONE_NAME,   # "awsteach.com"
        domain_name=DEMO_DOMAIN,             # "demo.awsteach.com"
        permissions_boundary=foundation.permissions_boundary,
    )
    cert.add_dependency(foundation)

The hosting stack (task 18.2, ``edge_hosting_stack.py``) consumes the
certificate ARN exposed as ``cert.certificate_arn`` (and the ``CfnOutput``
export ``PharmaPulse-EdgeCertificateArn``) to attach the cert to the
CloudFront distribution.

IMPORTANT: Because the hosted zone is looked up with
``HostedZone.from_lookup``, this stack must be given a concrete
account/region (via ``env=edge_env``) so the lookup can run against the
target account during synthesis.
===========================================================================
"""

from __future__ import annotations

from typing import Optional

from aws_cdk import (
    CfnOutput,
    Stack,
    Tags,
)
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_iam as iam
from aws_cdk import aws_route53 as route53
from constructs import Construct

from stacks.config import (
    DEMO_DOMAIN,
    HOSTED_ZONE_NAME,
    TAG_OWNER,
    TAG_OWNER_VALUE,
    TAG_PROJECT,
    TAG_PROJECT_VALUE,
)


class EdgeCertStack(Stack):
    """ACM certificate for ``demo.awsteach.com`` in us-east-1 (task 18.1)."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        hosted_zone_name: str = HOSTED_ZONE_NAME,
        domain_name: str = DEMO_DOMAIN,
        permissions_boundary: Optional[iam.IManagedPolicy] = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # -- Standard tags (Requirement 17.5 / 17.7) --------------------
        # The app-scope aspect in app.py already applies these to every
        # resource; re-applying at stack scope keeps the standard enforced
        # even when this stack is synthesized in isolation. Edge resources
        # follow the same tagging standard as primary-region resources
        # (Requirement 17.7).
        Tags.of(self).add(TAG_PROJECT, TAG_PROJECT_VALUE)
        Tags.of(self).add(TAG_OWNER, TAG_OWNER_VALUE)

        # -- Least-privilege boundary (Requirement 17.2 / 17.7) ---------
        # Attach the shared permissions boundary so any role created in this
        # stack (e.g. the DNS-validation custom-resource role that CDK adds
        # for ``CertificateValidation.from_dns``) cannot exceed the project
        # ceiling. The same least-privilege standard applies to edge
        # resources (Requirement 17.7).
        if permissions_boundary is not None:
            iam.PermissionsBoundary.of(self).apply(permissions_boundary)

        # -- Route 53 public hosted zone lookup (Requirement 14.5) ------
        # Look up the existing public hosted zone ``awsteach.com`` so the
        # certificate's DNS validation records are written into it. The
        # lookup requires a concrete account/region on the stack (supplied
        # via ``env=edge_env`` in app.py).
        self._hosted_zone = route53.HostedZone.from_lookup(
            self,
            "HostedZone",
            domain_name=hosted_zone_name,
        )

        # -- ACM certificate in us-east-1 (Requirement 14.4) ------------
        # DNS-validated against the hosted zone above so issuance is
        # automatic and non-interactive. This stack is pinned to us-east-1
        # (via ``env=edge_env``), which is the region CloudFront requires for
        # its viewer certificate (Requirement 14.4 / 17.6 edge exemption).
        self._certificate = acm.Certificate(
            self,
            "EdgeCertificate",
            domain_name=domain_name,
            validation=acm.CertificateValidation.from_dns(self._hosted_zone),
        )

        # -- Cross-stack output for the hosting stack (task 18.2) -------
        CfnOutput(
            self,
            "EdgeCertificateArn",
            value=self._certificate.certificate_arn,
            description=(
                "ACM certificate ARN (us-east-1) for the CloudFront "
                "distribution serving demo.awsteach.com (task 18.2)."
            ),
            export_name="PharmaPulse-EdgeCertificateArn",
        )

    # -- Public accessors for cross-stack reference ---------------------
    @property
    def certificate(self) -> acm.ICertificate:
        """The ACM certificate for the custom domain (CloudFront, us-east-1)."""
        return self._certificate

    @property
    def certificate_arn(self) -> str:
        """ARN of the ACM certificate for the hosting stack (task 18.2)."""
        return self._certificate.certificate_arn

    @property
    def hosted_zone(self) -> route53.IHostedZone:
        """The looked-up public hosted zone (awsteach.com)."""
        return self._hosted_zone
