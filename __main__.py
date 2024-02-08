import os
from typing import Optional

import pulumi
import pulumi_aws as aws
import pulumi_synced_folder as synced_folder


def get_pr_num() -> Optional[str]:
    pr_num = os.environ.get("PR_NUM")
    return pr_num or None


def get_frontend_host(config: pulumi.Config) -> str:
    base = config.require("frontend_host")
    pr_num = get_pr_num()
    return f"pr-{pr_num}.{base}" if pr_num else base


def stack(config: pulumi.Config):
    path = config.get("path") or "../frontend/dist"
    index_document = config.get("indexDocument") or "index.html"

    # Create an S3 bucket and configure it as a website.
    bucket = aws.s3.Bucket(
        "bucket",
        website=aws.s3.BucketWebsiteArgs(
            index_document=index_document,
            error_document=index_document,
        ),
    )

    # Set ownership controls for the new bucket
    ownership_controls = aws.s3.BucketOwnershipControls(
        "ownership-controls",
        bucket=bucket.bucket,
        rule=aws.s3.BucketOwnershipControlsRuleArgs(
            object_ownership="ObjectWriter",
        ),
    )

    # Configure public ACL block on the new bucket
    public_access_block = aws.s3.BucketPublicAccessBlock(
        "public-access-block",
        bucket=bucket.bucket,
        block_public_acls=False,
    )

    # Use a synced folder to manage the files of the website.
    synced_folder.S3BucketFolder(
        "bucket-folder",
        acl="public-read",
        bucket_name=bucket.bucket,
        path=path,
        opts=pulumi.ResourceOptions(
            depends_on=[ownership_controls, public_access_block]
        ),
    )

    us_east_1 = aws.Provider(
        "us-east-1",
        aws.ProviderArgs(
            region="us-east-1",
        ),
    )
    zone_host = config.require("zone_host")
    frontend_host = get_frontend_host(config)

    if not frontend_host.endswith(zone_host):
        raise ValueError("Frontend host doesn't match the zone host.")

    zone = aws.route53.get_zone(name=zone_host)

    cert = aws.acm.get_certificate(
        domain=config.require("cert_host"),
        most_recent=True,
        statuses=["ISSUED"],
        opts=pulumi.InvokeOptions(provider=us_east_1),
    )

    # Create a CloudFront CDN to distribute and cache the website.
    cdn = aws.cloudfront.Distribution(
        "cdn",
        enabled=True,
        aliases=[frontend_host],
        origins=[
            aws.cloudfront.DistributionOriginArgs(
                origin_id=bucket.arn,
                domain_name=bucket.website_endpoint,
                custom_origin_config=aws.cloudfront.DistributionOriginCustomOriginConfigArgs(
                    origin_protocol_policy="http-only",
                    http_port=80,
                    https_port=443,
                    origin_ssl_protocols=["TLSv1.2"],
                ),
            ),
        ],
        default_cache_behavior=aws.cloudfront.DistributionDefaultCacheBehaviorArgs(
            target_origin_id=bucket.arn,
            viewer_protocol_policy="redirect-to-https",
            allowed_methods=[
                "GET",
                "HEAD",
                "OPTIONS",
                "POST",
                "DELETE",
                "PUT",
                "PATCH",
            ],
            cached_methods=[
                "GET",
                "HEAD",
                "OPTIONS",
            ],
            default_ttl=600,
            max_ttl=600,
            min_ttl=600,
            forwarded_values=aws.cloudfront.DistributionDefaultCacheBehaviorForwardedValuesArgs(
                query_string=True,
                cookies=aws.cloudfront.DistributionDefaultCacheBehaviorForwardedValuesCookiesArgs(
                    forward="all",
                ),
                headers=["Origin"],
            ),
        ),
        price_class="PriceClass_100",
        restrictions=aws.cloudfront.DistributionRestrictionsArgs(
            geo_restriction=aws.cloudfront.DistributionRestrictionsGeoRestrictionArgs(
                restriction_type="none",
            ),
        ),
        viewer_certificate=aws.cloudfront.DistributionViewerCertificateArgs(
            cloudfront_default_certificate=False,
            acm_certificate_arn=cert.arn,
            ssl_support_method="sni-only",
        ),
    )

    # Create a DNS A record to point to the CDN.
    aws.route53.Record(
        "bucketRedirect",
        name=frontend_host[: -len(zone_host)].strip("."),
        zone_id=zone.zone_id,
        type="A",
        aliases=[
            aws.route53.RecordAliasArgs(
                name=cdn.domain_name,
                zone_id=cdn.hosted_zone_id,
                evaluate_target_health=True,
            )
        ],
    )

    # Export the URLs and hostnames of the bucket and distribution.
    pulumi.export("originURL", pulumi.Output.concat("http://", bucket.website_endpoint))
    pulumi.export("originHostname", bucket.website_endpoint)
    pulumi.export("cdnURL", pulumi.Output.concat("https://", cdn.domain_name))
    pulumi.export("cdnHostname", cdn.domain_name)
    pulumi.export("aliasURL", pulumi.Output.concat("https://", frontend_host))


# Import the program's configuration settings.
config = pulumi.Config()
stack(config)
