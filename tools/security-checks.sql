-- Generic AWS security-posture monitoring queries against a DuckDB instance
-- built by tools/build-duckdb.py from a tools/inventory.sh run.
--
-- No account IDs, customer names, or other real-infrastructure identifiers
-- are hardcoded here -- every query is written against ajl's generic
-- output.resources table/column shapes, so it's safe to run against any
-- account's inventory and safe to keep in a public repo. Re-run this file
-- against a freshly-rebuilt DuckDB file after remediation work to check
-- progress: an empty result set means that finding is resolved (or was
-- never present); rows mean it's still open.
--
-- Usage:
--   duckdb .temp/inventory.duckdb < tools/security-checks.sql
-- or, to keep each query's output clearly separated:
--   duckdb -box .temp/inventory.duckdb < tools/security-checks.sql
--
-- Tables referenced here only exist if the matching tools/inventory.sh
-- section produced non-empty output for the account(s) loaded -- a missing
-- table (rather than an empty result set) usually just means that resource
-- type wasn't present, or that section of inventory.sh hasn't been run yet.


-- ============================================================================
-- 1. EKS clusters with a public API endpoint and no private fallback
--    Worst of the three possible EndpointPublicAccess/EndpointPrivateAccess
--    configurations: public open to 0.0.0.0/0 with nothing private to fall
--    back to. Compare against clusters that restrict publicAccessCidrs to
--    known IPs, or disable public access and rely on private+bastion/VPN.
-- ============================================================================
SELECT
    ajl.name AS cluster_name,
    ajl.stamp.account AS account,
    ajl.stamp.region AS region,
    resourcesVpcConfig.endpointPublicAccess AS public_access,
    resourcesVpcConfig.endpointPrivateAccess AS private_access,
    resourcesVpcConfig.publicAccessCidrs AS public_cidrs
FROM eks_clusters
WHERE resourcesVpcConfig.endpointPublicAccess = true
  AND resourcesVpcConfig.endpointPrivateAccess = false
  AND list_contains(resourcesVpcConfig.publicAccessCidrs, '0.0.0.0/0');


-- ============================================================================
-- 2a. Expired ACM certificates still bound to a live resource (Critical)
--     InUseBy is non-empty -- something is actively configured to use an
--     expired cert (Client VPN endpoints, ALB listeners, CloudFront, ...).
-- ============================================================================
SELECT
    ajl.name AS domain,
    ajl.stamp.account AS account,
    Status,
    RenewalEligibility,
    NotAfter AS expired_on,
    InUseBy
FROM acm_certificates
WHERE Status = 'EXPIRED'
  AND len(InUseBy) > 0;

-- 2b. Expired ACM certificates with nothing using them (hygiene only)
SELECT
    ajl.name AS domain,
    ajl.stamp.account AS account,
    Status,
    NotAfter AS expired_on
FROM acm_certificates
WHERE Status = 'EXPIRED'
  AND len(InUseBy) = 0;


-- ============================================================================
-- 3. EC2 instances whose instance profile grants AdministratorAccess
--    Not inherently wrong (break-glass roles exist), but worth an explicit
--    look at every hit -- especially anything stopped/idle, which usually
--    means "forgotten," not "in active use."
-- ============================================================================
WITH admin_roles AS (
    SELECT DISTINCT ajl.stamp.RoleName AS role_name
    FROM iam_attached_role_policies
    WHERE PolicyArn LIKE '%/AdministratorAccess'
),
profile_roles AS (
    SELECT Arn AS profile_arn, unnest(Roles).RoleName AS role_name
    FROM iam_instance_profiles
)
SELECT
    i.ajl.stamp.account AS account,
    i.ajl.id AS instance_id,
    i.State."Name" AS state,
    i.LaunchTime AS launched,
    pr.role_name
FROM ec2_instances i
JOIN profile_roles pr ON pr.profile_arn = i.IamInstanceProfile.Arn
JOIN admin_roles ar ON ar.role_name = pr.role_name;


-- ============================================================================
-- 4. RDS instances reachable from 0.0.0.0/0 via one of their security groups
--    Cross-check against PubliclyAccessible/StorageEncrypted below --
--    0.0.0.0/0 on a private-only, encrypted instance is a defense-in-depth
--    gap, not a direct exposure; 0.0.0.0/0 + PubliclyAccessible=true is.
-- ============================================================================
WITH rds_sgs AS (
    SELECT DISTINCT
        ajl.stamp.account AS account,
        ajl.id AS db_id,
        PubliclyAccessible,
        StorageEncrypted,
        unnest(VpcSecurityGroups).VpcSecurityGroupId AS sg_id,
        Endpoint.Port AS db_port
    FROM rds_db_instances
)
SELECT
    r.account, r.db_id, r.db_port, sg.GroupName AS sg_name,
    r.PubliclyAccessible, r.StorageEncrypted
FROM rds_sgs r
JOIN ec2_security_groups sg ON sg.GroupId = r.sg_id
WHERE EXISTS (
    SELECT 1 FROM unnest(flatten(list_transform(sg.IpPermissions, lambda p: p.IpRanges))) AS t(rg)
    WHERE t.rg.CidrIp = '0.0.0.0/0'
)
ORDER BY r.PubliclyAccessible DESC, r.account, r.db_id;


-- ============================================================================
-- 5. CloudTrail trails with log file validation disabled
--    Free tamper-detection with no real downside -- there's rarely a reason
--    for this to stay off. DISTINCT collapses the one-row-per-region-session
--    duplication multi-region trails get from a `--all`-fanned inventory.
-- ============================================================================
SELECT DISTINCT
    ajl.name AS trail,
    ajl.stamp.account AS account,
    S3BucketName,
    IsMultiRegionTrail
FROM cloudtrail_trails
WHERE LogFileValidationEnabled = false;


-- ============================================================================
-- 6. CloudTrail destination buckets: versioning + lifecycle posture
--    A trail with no matching s3_bucket_lifecycle_configuration row either
--    has no lifecycle rule (retains forever, by omission rather than
--    decision) or hasn't been scanned by that inventory.sh section yet --
--    check which before reading too much into a NULL here.
-- ============================================================================
SELECT DISTINCT
    t.ajl.name AS trail,
    t.ajl.stamp.account AS account,
    t.S3BucketName AS bucket,
    v.Status AS versioning_status,
    lc.ajl.name IS NOT NULL AS has_lifecycle_rule
FROM cloudtrail_trails t
LEFT JOIN s3_bucket_versioning v ON v.ajl.stamp.Bucket = t.S3BucketName
LEFT JOIN s3_bucket_lifecycle_configuration lc ON lc.ajl.stamp.Bucket = t.S3BucketName;


-- ============================================================================
-- 7. Security groups open to 0.0.0.0/0 that aren't attached to anything
--    The "landmine" case: a live, dangerous rule with nothing currently
--    depending on it, so deleting it is low-risk and worth doing on sight.
--    (Compare against #4 above for open-but-*in-use* SGs, which need the
--    Terraform-module-level fix instead of a delete.)
-- ============================================================================
WITH attached AS (
    SELECT DISTINCT unnest(SecurityGroups).GroupId AS sg_id FROM ec2_instances
    UNION
    SELECT DISTINCT unnest(Groups).GroupId AS sg_id FROM ec2_network_interfaces
)
SELECT sg.ajl.stamp.account AS account, sg.GroupId, sg.GroupName, sg.VpcId
FROM ec2_security_groups sg
WHERE sg.GroupId NOT IN (SELECT sg_id FROM attached)
  AND EXISTS (
      SELECT 1 FROM unnest(flatten(list_transform(sg.IpPermissions, lambda p: p.IpRanges))) AS t(rg)
      WHERE t.rg.CidrIp = '0.0.0.0/0'
  );


-- ============================================================================
-- 8. S3 buckets without versioning enabled
--    Durability/ransomware-recovery gap, not an access-control issue --
--    cross-check public-access-block/encryption separately, those matter more.
-- ============================================================================
SELECT DISTINCT b.ajl.name AS bucket, b.ajl.stamp.account AS account
FROM s3_buckets b
LEFT JOIN s3_bucket_versioning v ON v.ajl.stamp.Bucket = b.ajl.name
WHERE v.Status IS NULL OR v.Status != 'Enabled';


-- ============================================================================
-- 9. RDS instances without Multi-AZ, by account and AZ
--    Surfaces both the overall count and the concentration-risk angle: a
--    large non_multi_az_count in one AZ means one AZ outage takes out that
--    many databases at once, not just "some databases have no failover."
-- ============================================================================
SELECT
    ajl.stamp.account AS account,
    AvailabilityZone,
    count(*) AS instance_count,
    sum(CASE WHEN MultiAZ THEN 0 ELSE 1 END) AS non_multi_az_count
FROM rds_db_instances
GROUP BY 1, 2
HAVING sum(CASE WHEN MultiAZ THEN 0 ELSE 1 END) > 0
ORDER BY non_multi_az_count DESC;


-- ============================================================================
-- 10. IAM access keys past a rotation/dormancy threshold (90 days)
--     Flags: keys older than 90 days regardless of use, keys unused for
--     90+ days despite being Active, and keys with no recorded last-use at
--     all (GetAccessKeyLastUsed never echoes a use -- likely never used, or
--     used somewhere CloudTrail-invisible). Adjust the two `90`s to your
--     actual rotation policy once one exists.
-- ============================================================================
SELECT
    k.ajl.stamp.account AS account,
    k.UserName,
    k.AccessKeyId,
    date_diff('day', k.CreateDate, current_date) AS key_age_days,
    date_diff('day', u.AccessKeyLastUsed.LastUsedDate, current_date) AS days_since_last_used
FROM iam_access_keys k
LEFT JOIN iam_access_key_last_used u ON u.ajl.stamp.AccessKeyId = k.AccessKeyId
WHERE k.Status = 'Active'
  AND (
      date_diff('day', k.CreateDate, current_date) > 90
      OR date_diff('day', u.AccessKeyLastUsed.LastUsedDate, current_date) > 90
      OR u.AccessKeyLastUsed.LastUsedDate IS NULL
  )
ORDER BY key_age_days DESC;


-- ============================================================================
-- 11. Secrets Manager rotation coverage
--     One aggregate number to watch trend on as rotation gets rolled out,
--     rather than a per-secret list (7000+ secrets in a large account).
-- ============================================================================
SELECT
    count(*) AS total_secrets,
    sum(CASE WHEN RotationEnabled THEN 1 ELSE 0 END) AS rotation_enabled,
    round(100.0 * sum(CASE WHEN RotationEnabled THEN 1 ELSE 0 END) / count(*), 2) AS pct_enabled
FROM secretsmanager_secrets;


-- ============================================================================
-- Not yet checkable from this inventory (curation gaps, not query gaps):
--
-- KMS automatic key rotation status -- needs `kms.GetKeyRotationStatus`
-- curated and wired into inventory.sh (currently only ListKeys/DescribeKey/
-- ListAliases/ListKeyPolicies are). Once present, the query is simply:
--   SELECT ajl.name, ajl.stamp.account
--   FROM kms_key_rotation_status WHERE KeyRotationEnabled = false;
--
-- IAM MFA enforcement per user -- `iam.ListMFADevices` is curated but the
-- inventory.sh section has come back empty every run so far; worth a
-- `--describe` pairing against real users before trusting an empty table
-- here means "no one has MFA" rather than "this section isn't wired right."
--
-- WAFv2 rule coverage / default-action posture -- `wafv2_web_acls_regional`
-- exists but only carries Name/Id/ARN; the actual rule list and default
-- action live in `GetWebACL`'s response, which needs curating before a
-- posture query is possible here.
-- ============================================================================
