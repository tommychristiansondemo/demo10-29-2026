"""Edge hosting / content-delivery CDK stack for PharmaPulse (task 18.2).

Serves the React SPA over a secure, custom-domain content-delivery stack in
the Edge_Region (us-east-1): a PRIVATE S3 origin reachable only through
CloudFront via Origin Access Control (OAC), an attached AWS WAF Web ACL, the
us-east-1 ACM certificate as the viewer certificate, and a Route 53 alias in
the ``awsteach.com`` hosted zone resolving ``demo.awsteach.com`` to the
distribution. CloudFront requires its viewer certificate, the distribution,
and the CLOUDFRONT-scope WAF to live in us-east-1, so this stack is pinned to
the Edge_Region (Requirement 17.6 edge exemption); least-privilege, encryption,
and tagging standards still apply uniformly (Requirement 17.7).

Requirements covered:
  * 14.1 - private S3 bucket, not publicly accessible, holds the SPA assets.
  * 14.2 - CloudFront reaches the private bucket via Origin Access Control.
  * 14.3 - AWS WAF Web ACL attached to the CloudFront distribution.
  * 14.5 - Route 53 alias record resolves demo.awsteach.com to CloudFront.
  * 17.7 - standard tags + permissions boundary on edge resources.

===========================================================================
app.py WIRING (do NOT edit app.py in task 18.2 — task 14.2 is editing it
concurrently). Wire this stack in AFTER the ``cert`` (EdgeCertStack) and
``foundation`` stacks exist, using the edge environment (us-east-1):

    from stacks.edge_hosting_stack import EdgeHostingStack

    hosting = EdgeHostingStack(
        app,
        "PharmaPulse-EdgeHosting",
        env=edge_env,  # us-east-1 (Requirement 14 / 17.6)
        certificate=cert.certificate,          # acm.ICertificate (us-east-1)
        hosted_zone=cert.hosted_zone,          # route53.IHostedZone (awsteach.com)
        permissions_boundary=foundation.permissions_boundary,
        # domain_name / spa_asset_dir default to DEMO_DOMAIN / "frontend/dist".
    )
    hosting.add_dependency(cert)

The distribution domain + URL are exposed as ``hosting.distribution_domain_name``
and ``hosting.distribution_url`` (and the ``PharmaPulse-EdgeDistribution*``
CfnOutputs).

IMPORTANT - SPA assets: the BucketDeployment sources the build output of the
React SPA from ``frontend/dist`` (produced by ``npm run build`` under
``frontend/``). That directory MUST exist at synth time (CDK fingerprints the
asset during synthesis). If it does not, run ``npm --prefix frontend run build``
first, or synth will fail with a missing-asset error. When ``frontend/dist`` is
absent this stack still constructs (the deployment is guarded) so the module
can be imported/synthesized in isolation during development; the guard is
logged via a CfnOutput so the omission is visible.
===========================================================================
"""

from __future__ import annotations

import os
from typing import Optional

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    Tags,
)
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_iam as iam
from aws_cdk import aws_route53 as route53
from aws_cdk import aws_route53_targets as route53_targets
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_deployment as s3_deployment
from aws_cdk import aws_wafv2 as wafv2
from constructs import Construct

from stacks.config import (
    DEMO_DOMAIN,
    TAG_OWNER,
    TAG_OWNER_VALUE,
    TAG_PROJECT,
    TAG_PROJECT_VALUE,
)

# Default build-output directory of the React SPA (``npm run build`` under
# frontend/). Resolved relative to the repository root (two levels up from
# this file: stacks/ -> repo root).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_SPA_ASSET_DIR = os.path.join("frontend", "dist")


class EdgeHostingStack(Stack):
    """Private S3 + CloudFront (OAC) + WAF + Route 53 for the SPA (task 18.2)."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        certificate: acm.ICertificate,
        hosted_zone: route53.IHostedZone,
        permissions_boundary: Optional[iam.IManagedPolicy] = None,
        domain_name: str = DEMO_DOMAIN,
        spa_asset_dir: str = _DEFAULT_SPA_ASSET_DIR,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # -- Standard tags (Requirement 17.5 / 17.7) --------------------
        # Re-applied at stack scope so the standard holds even when this
        # stack is synthesized in isolation. Edge resources follow the same
        # tagging standard as primary-region resources (Requirement 17.7).
        Tags.of(self).add(TAG_PROJECT, TAG_PROJECT_VALUE)
        Tags.of(self).add(TAG_OWNER, TAG_OWNER_VALUE)

        # -- Least-privilege boundary (Requirement 17.2 / 17.7) ---------
        # Attach the shared permissions boundary so any role created here
        # (e.g. the BucketDeployment custom-resource Lambda role) cannot
        # exceed the project ceiling.
        if permissions_boundary is not None:
            iam.PermissionsBoundary.of(self).apply(permissions_boundary)

        # -- Private S3 origin bucket (Requirement 14.1) ----------------
        # NOT public: block ALL public access and enforce SSL. Reachable only
        # through CloudFront via OAC (granted below). S3-managed SSE provides
        # encryption at rest (Requirement 17.3 / 17.7). Objects auto-delete on
        # stack removal so the demo is cleanly tearable (Requirement 18.4).
        self._bucket = s3.Bucket(
            self,
            "SpaOriginBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            public_read_access=False,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        # -- AWS WAF Web ACL (Requirement 14.3) -------------------------
        # CLOUDFRONT-scope Web ACLs MUST be created in us-east-1 (this stack
        # is pinned to the Edge_Region). Default-allow with the AWS managed
        # common rule set so the distribution has a baseline WAF attached.
        self._web_acl = wafv2.CfnWebACL(
            self,
            "SpaWebAcl",
            scope="CLOUDFRONT",
            default_action=wafv2.CfnWebACL.DefaultActionProperty(
                allow=wafv2.CfnWebACL.AllowActionProperty()
            ),
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                cloud_watch_metrics_enabled=True,
                metric_name="PharmaPulseSpaWebAcl",
                sampled_requests_enabled=True,
            ),
            rules=[
                wafv2.CfnWebACL.RuleProperty(
                    name="AWS-AWSManagedRulesCommonRuleSet",
                    priority=0,
                    override_action=wafv2.CfnWebACL.OverrideActionProperty(
                        none={}
                    ),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        managed_rule_group_statement=(
                            wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                                vendor_name="AWS",
                                name="AWSManagedRulesCommonRuleSet",
                            )
                        )
                    ),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        cloud_watch_metrics_enabled=True,
                        metric_name="AWSManagedRulesCommonRuleSet",
                        sampled_requests_enabled=True,
                    ),
                )
            ],
        )

        # -- CloudFront distribution with OAC (Requirements 14.2, 14.3) -
        # ``S3BucketOrigin.with_origin_access_control`` provisions an Origin
        # Access Control and wires the bucket policy so the PRIVATE bucket is
        # reachable ONLY through this distribution (Requirement 14.2). The
        # us-east-1 ACM cert is the viewer certificate and demo.awsteach.com
        # is an alias. The WAF Web ACL is attached by ARN (Requirement 14.3).
        self._distribution = cloudfront.Distribution(
            self,
            "SpaDistribution",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(
                    self._bucket
                ),
                viewer_protocol_policy=(
                    cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS
                ),
            ),
            domain_names=[domain_name],
            certificate=certificate,
            default_root_object="index.html",
            web_acl_id=self._web_acl.attr_arn,
            minimum_protocol_version=(
                cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021
            ),
            # SPA client-side routing: serve index.html for 403/404 so deep
            # links resolve to the React app shell.
            error_responses=[
                cloudfront.ErrorResponse(
                    http_status=403,
                    response_http_status=200,
                    response_page_path="/index.html",
                    ttl=Duration.seconds(10),
                ),
                cloudfront.ErrorResponse(
                    http_status=404,
                    response_http_status=200,
                    response_page_path="/index.html",
                    ttl=Duration.seconds(10),
                ),
            ],
        )

        # -- Deploy the built SPA assets into the private bucket (14.1) --
        # Source the React build output (``frontend/dist`` via ``npm run
        # build``). The asset directory must exist at synth time; guard
        # gracefully when absent so the module imports/synthesizes in
        # isolation during development.
        abs_asset_dir = (
            spa_asset_dir
            if os.path.isabs(spa_asset_dir)
            else os.path.join(_REPO_ROOT, spa_asset_dir)
        )
        if os.path.isdir(abs_asset_dir):
            s3_deployment.BucketDeployment(
                self,
                "SpaAssetsDeployment",
                sources=[s3_deployment.Source.asset(abs_asset_dir)],
                destination_bucket=self._bucket,
                distribution=self._distribution,
                distribution_paths=["/*"],
                prune=True,
            )
            self._assets_deployed = True
        else:
            # Guard: no build output yet. Record the omission visibly so it is
            # obvious ``npm run build`` must run before a real deploy.
            self._assets_deployed = False
            CfnOutput(
                self,
                "SpaAssetsNotDeployed",
                value=(
                    f"SPA assets NOT deployed: '{spa_asset_dir}' not found. "
                    "Run `npm --prefix frontend run build` before deploying."
                ),
                description=(
                    "SPA build output was missing at synth time (task 18.2). "
                    "The S3 origin bucket was created empty."
                ),
            )

        # -- Route 53 alias: demo.awsteach.com -> CloudFront (14.5) -----
        # A/AAAA alias records in the awsteach.com public hosted zone so the
        # Demo_Domain resolves to the distribution.
        cloudfront_target = route53.RecordTarget.from_alias(
            route53_targets.CloudFrontTarget(self._distribution)
        )
        record_name = _record_name_for(domain_name, hosted_zone.zone_name)

        route53.ARecord(
            self,
            "SpaAliasA",
            zone=hosted_zone,
            record_name=record_name,
            target=cloudfront_target,
        )
        route53.AaaaRecord(
            self,
            "SpaAliasAAAA",
            zone=hosted_zone,
            record_name=record_name,
            target=cloudfront_target,
        )

        # -- Cross-stack / operational outputs --------------------------
        CfnOutput(
            self,
            "EdgeDistributionDomainName",
            value=self._distribution.distribution_domain_name,
            description="CloudFront distribution domain name (task 18.2).",
            export_name="PharmaPulse-EdgeDistributionDomainName",
        )
        CfnOutput(
            self,
            "EdgeDistributionUrl",
            value=f"https://{domain_name}",
            description=(
                "Public URL served via the CloudFront distribution "
                "(demo.awsteach.com) (task 18.2 / Requirement 20.2)."
            ),
            export_name="PharmaPulse-EdgeDistributionUrl",
        )
        CfnOutput(
            self,
            "EdgeWebAclArn",
            value=self._web_acl.attr_arn,
            description="AWS WAF Web ACL ARN attached to CloudFront (14.3).",
            export_name="PharmaPulse-EdgeWebAclArn",
        )

    # -- Public accessors for cross-stack reference ---------------------
    @property
    def bucket(self) -> s3.IBucket:
        """The private S3 origin bucket holding the SPA assets (Req 14.1)."""
        return self._bucket

    @property
    def distribution(self) -> cloudfront.IDistribution:
        """The CloudFront distribution serving the SPA (Req 14.2)."""
        return self._distribution

    @property
    def distribution_domain_name(self) -> str:
        """CloudFront-assigned distribution domain name."""
        return self._distribution.distribution_domain_name

    @property
    def distribution_url(self) -> str:
        """Public custom-domain URL (https://demo.awsteach.com)."""
        return f"https://{DEMO_DOMAIN}"

    @property
    def web_acl(self) -> wafv2.CfnWebACL:
        """The CLOUDFRONT-scope AWS WAF Web ACL (Req 14.3)."""
        return self._web_acl

    @property
    def assets_deployed(self) -> bool:
        """True when the SPA build output existed and was deployed."""
        return self._assets_deployed


def _record_name_for(domain_name: str, zone_name: str) -> Optional[str]:
    """Compute the Route 53 record name relative to the hosted zone.

    For ``demo.awsteach.com`` in zone ``awsteach.com`` this returns ``demo``;
    when the domain equals the zone apex it returns ``None`` (zone root).
    """
    domain = domain_name.rstrip(".")
    zone = zone_name.rstrip(".")
    if domain == zone:
        return None
    suffix = "." + zone
    if domain.endswith(suffix):
        return domain[: -len(suffix)]
    # Fall back to the full domain if it is not a subdomain of the zone.
    return domain
