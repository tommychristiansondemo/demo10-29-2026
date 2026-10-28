"""CDK assertion tests for the edge hosting / content-delivery stacks (task 18.3).

These tests synthesize the edge stacks into throwaway CDK ``App`` instances
pinned to the Edge_Region (us-east-1) and assert the hosting engineering
standards from Requirement 14 by inspecting the synthesized CloudFormation
with ``aws_cdk.assertions.Template`` — no AWS calls are made, so they run
fast and offline.

Requirement coverage:
  * 14.1 - the SPA origin is a PRIVATE S3 bucket (BlockPublicAccess BLOCK_ALL).
  * 14.2 - CloudFront reaches the private bucket via Origin Access Control
    (an ``AWS::CloudFront::OriginAccessControl`` exists, the distribution origin
    uses it, and the bucket policy grants ``cloudfront.amazonaws.com``).
  * 14.3 - a CLOUDFRONT-scope AWS WAF Web ACL exists and the distribution's
    ``DistributionConfig`` carries a ``WebACLId``.
  * 14.4 - the distribution's viewer certificate references an ACM cert ARN,
    and the ``EdgeCertStack`` provisions an ACM certificate in us-east-1 for
    ``demo.awsteach.com``.
  * 14.5 - Route 53 A + AAAA alias records in the ``awsteach.com`` zone point
    ``demo.awsteach.com`` (record name ``demo``) at the CloudFront distribution.

The hosting stack is built hermetically: instead of relying on
``HostedZone.from_lookup`` (which needs live account context), it is handed a
synthetic ACM certificate (``Certificate.from_certificate_arn``) and a synthetic
hosted zone (``HostedZone.from_hosted_zone_attributes``) so the template is
produced deterministically offline. A separate, context-seeded ``EdgeCertStack``
is synthesized to assert the ACM certificate's us-east-1 placement (14.4).
"""

from __future__ import annotations

import aws_cdk as cdk
import pytest
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_route53 as route53
from aws_cdk.assertions import Match, Template

from stacks.config import DEMO_DOMAIN, EDGE_REGION, HOSTED_ZONE_NAME
from stacks.edge_cert_stack import EdgeCertStack
from stacks.edge_hosting_stack import EdgeHostingStack

_TEST_ACCOUNT = "123456789012"
_FAKE_CERT_ARN = (
    "arn:aws:acm:us-east-1:123456789012:certificate/"
    "11111111-2222-3333-4444-555555555555"
)
_FAKE_ZONE_ID = "Z0123456789ABCDEFGHIJ"


@pytest.fixture(scope="module")
def hosting_template():
    """Synthesize an ``EdgeHostingStack`` hermetically in us-east-1.

    A synthetic ACM certificate and hosted zone are supplied so the stack
    synthesizes offline (no ``from_lookup`` context dependency), mirroring how
    ``app.py`` wires ``certificate`` + ``hosted_zone`` into the stack.
    """
    app = cdk.App()
    env = cdk.Environment(account=_TEST_ACCOUNT, region=EDGE_REGION)

    # Build a tiny holder stack for the imported certificate/zone so the
    # ``from_*`` constructs have a scope with a concrete environment.
    ref_stack = cdk.Stack(app, "RefStack", env=env)
    certificate = acm.Certificate.from_certificate_arn(
        ref_stack, "ImportedCert", _FAKE_CERT_ARN
    )
    hosted_zone = route53.HostedZone.from_hosted_zone_attributes(
        ref_stack,
        "ImportedZone",
        hosted_zone_id=_FAKE_ZONE_ID,
        zone_name=HOSTED_ZONE_NAME,
    )

    hosting = EdgeHostingStack(
        app,
        "TestEdgeHosting",
        env=env,
        certificate=certificate,
        hosted_zone=hosted_zone,
    )
    return hosting, Template.from_stack(hosting)


@pytest.fixture(scope="module")
def cert_stack():
    """Synthesize an ``EdgeCertStack`` in us-east-1 with seeded zone context.

    The ACM certificate's DNS validation looks up the ``awsteach.com`` hosted
    zone via ``HostedZone.from_lookup``. Seeding the lookup result into the
    app context lets the stack synthesize offline while still placing the
    certificate in the Edge_Region (Requirement 14.4).
    """
    app = cdk.App(
        context={
            # Context key format for a Route 53 hosted-zone lookup. Seeding it
            # avoids a live AWS call during synthesis.
            f"hosted-zone:account={_TEST_ACCOUNT}:domainName={HOSTED_ZONE_NAME}"
            f":region={EDGE_REGION}": {
                "Id": f"/hostedzone/{_FAKE_ZONE_ID}",
                "Name": f"{HOSTED_ZONE_NAME}.",
            }
        }
    )
    env = cdk.Environment(account=_TEST_ACCOUNT, region=EDGE_REGION)
    cert = EdgeCertStack(app, "TestEdgeCert", env=env)
    return cert, Template.from_stack(cert)


class TestPrivateS3Origin:
    """Requirement 14.1 — the SPA origin bucket is private."""

    def test_single_origin_bucket_exists(self, hosting_template):
        _, template = hosting_template
        template.resource_count_is("AWS::S3::Bucket", 1)

    def test_bucket_blocks_all_public_access(self, hosting_template):
        _, template = hosting_template
        # All four public-access-block switches must be true so the bucket is
        # not publicly accessible in any form.
        template.has_resource_properties(
            "AWS::S3::Bucket",
            {
                "PublicAccessBlockConfiguration": {
                    "BlockPublicAcls": True,
                    "BlockPublicPolicy": True,
                    "IgnorePublicAcls": True,
                    "RestrictPublicBuckets": True,
                }
            },
        )

    def test_bucket_is_encrypted_at_rest(self, hosting_template):
        _, template = hosting_template
        # A private origin should also encrypt at rest (defense in depth).
        template.has_resource_properties(
            "AWS::S3::Bucket",
            {
                "BucketEncryption": Match.object_like(
                    {"ServerSideEncryptionConfiguration": Match.any_value()}
                )
            },
        )


class TestOriginAccessControl:
    """Requirement 14.2 — CloudFront reaches the private bucket via OAC."""

    def test_origin_access_control_resource_exists(self, hosting_template):
        _, template = hosting_template
        template.resource_count_is(
            "AWS::CloudFront::OriginAccessControl", 1
        )

    def test_oac_signs_s3_origin_requests(self, hosting_template):
        _, template = hosting_template
        template.has_resource_properties(
            "AWS::CloudFront::OriginAccessControl",
            {
                "OriginAccessControlConfig": Match.object_like(
                    {
                        "OriginAccessControlOriginType": "s3",
                        "SigningBehavior": "always",
                        "SigningProtocol": "sigv4",
                    }
                )
            },
        )

    def test_distribution_origin_references_oac(self, hosting_template):
        _, template = hosting_template
        # The distribution's single origin must carry an OriginAccessControlId
        # (wiring the OAC to the S3 origin) rather than a legacy OAI.
        template.has_resource_properties(
            "AWS::CloudFront::Distribution",
            {
                "DistributionConfig": Match.object_like(
                    {
                        "Origins": Match.array_with(
                            [
                                Match.object_like(
                                    {
                                        "OriginAccessControlId": Match.any_value(),
                                        "S3OriginConfig": Match.any_value(),
                                    }
                                )
                            ]
                        )
                    }
                )
            },
        )

    def test_bucket_policy_grants_cloudfront_service(self, hosting_template):
        _, template = hosting_template
        # OAC access is granted to the CloudFront service principal in the
        # bucket policy so only the distribution can read the private objects.
        template.has_resource_properties(
            "AWS::S3::BucketPolicy",
            {
                "PolicyDocument": Match.object_like(
                    {
                        "Statement": Match.array_with(
                            [
                                Match.object_like(
                                    {
                                        "Action": "s3:GetObject",
                                        "Principal": {
                                            "Service": "cloudfront.amazonaws.com"
                                        },
                                    }
                                )
                            ]
                        )
                    }
                )
            },
        )


class TestWafAttached:
    """Requirement 14.3 — AWS WAF attached to the CloudFront distribution."""

    def test_cloudfront_scope_web_acl_exists(self, hosting_template):
        _, template = hosting_template
        template.has_resource_properties(
            "AWS::WAFv2::WebACL",
            {"Scope": "CLOUDFRONT"},
        )

    def test_distribution_carries_web_acl_id(self, hosting_template):
        _, template = hosting_template
        # The distribution config must reference a Web ACL (by ARN/Id). The WAF
        # ARN is produced via Fn::GetAtt on the CfnWebACL, so match any value.
        template.has_resource_properties(
            "AWS::CloudFront::Distribution",
            {
                "DistributionConfig": Match.object_like(
                    {"WebACLId": Match.any_value()}
                )
            },
        )


class TestViewerCertificate:
    """Requirement 14.4 — ACM viewer certificate (provisioned in us-east-1)."""

    def test_distribution_uses_acm_viewer_certificate(self, hosting_template):
        _, template = hosting_template
        # The distribution's viewer certificate must reference an ACM cert ARN
        # (our imported us-east-1 certificate) with a modern TLS policy.
        template.has_resource_properties(
            "AWS::CloudFront::Distribution",
            {
                "DistributionConfig": Match.object_like(
                    {
                        "ViewerCertificate": Match.object_like(
                            {
                                "AcmCertificateArn": _FAKE_CERT_ARN,
                                "SslSupportMethod": "sni-only",
                            }
                        )
                    }
                )
            },
        )

    def test_distribution_serves_custom_domain_alias(self, hosting_template):
        _, template = hosting_template
        template.has_resource_properties(
            "AWS::CloudFront::Distribution",
            {
                "DistributionConfig": Match.object_like(
                    {"Aliases": Match.array_with([DEMO_DOMAIN])}
                )
            },
        )

    def test_edge_cert_stack_region_is_us_east_1(self, cert_stack):
        cert, _ = cert_stack
        # The certificate stack is pinned to the Edge_Region so CloudFront can
        # consume the viewer certificate (Requirement 14.4 / 17.6).
        assert cert.region == EDGE_REGION == "us-east-1"

    def test_edge_cert_stack_provisions_acm_cert_for_demo_domain(
        self, cert_stack
    ):
        _, template = cert_stack
        template.resource_count_is(
            "AWS::CertificateManager::Certificate", 1
        )
        template.has_resource_properties(
            "AWS::CertificateManager::Certificate",
            {"DomainName": DEMO_DOMAIN},
        )


class TestRoute53Alias:
    """Requirement 14.5 — Route 53 alias resolves the Demo_Domain to CloudFront."""

    def test_a_and_aaaa_records_exist(self, hosting_template):
        _, template = hosting_template
        records = template.find_resources("AWS::Route53::RecordSet")
        types = sorted(r["Properties"].get("Type") for r in records.values())
        assert types == ["A", "AAAA"], (
            f"expected exactly one A and one AAAA record, got {types}"
        )

    def test_alias_record_name_is_demo_in_awsteach_zone(self, hosting_template):
        _, template = hosting_template
        # Record name is the fully-qualified Demo_Domain (CDK appends the zone
        # apex + trailing dot to the relative "demo" label).
        expected_name = f"{DEMO_DOMAIN}."
        template.has_resource_properties(
            "AWS::Route53::RecordSet",
            {
                "Type": "A",
                "Name": expected_name,
                "AliasTarget": Match.object_like(
                    {"DNSName": Match.any_value()}
                ),
            },
        )

    def test_alias_targets_cloudfront_distribution(self, hosting_template):
        _, template = hosting_template
        # The alias target DNSName must resolve to the CloudFront distribution
        # domain (GetAtt DomainName on the Distribution). The CloudFront hosted
        # zone id is injected via an Fn::FindInMap region mapping, so assert it
        # is present without pinning the literal mapping form.
        for record_type in ("A", "AAAA"):
            template.has_resource_properties(
                "AWS::Route53::RecordSet",
                {
                    "Type": record_type,
                    "AliasTarget": Match.object_like(
                        {
                            "DNSName": {
                                "Fn::GetAtt": [
                                    Match.string_like_regexp("SpaDistribution.*"),
                                    "DomainName",
                                ]
                            },
                            "HostedZoneId": Match.any_value(),
                        }
                    ),
                },
            )
