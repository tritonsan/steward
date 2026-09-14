# Steward mail DNS

These records were added in Netlify DNS on 2026-09-10. Existing root-domain and Factor mail records were preserved.

| Type | Name | Value |
|---|---|---|
| CNAME | nndamve6ltludwzihidmnac57ujpgen5._domainkey.steward.narrativenode-labs.cloud | nndamve6ltludwzihidmnac57ujpgen5.dkim.amazonses.com |
| CNAME | vjvv3u34ngorg6elcnnhfky2ogzgh32x._domainkey.steward.narrativenode-labs.cloud | vjvv3u34ngorg6elcnnhfky2ogzgh32x.dkim.amazonses.com |
| CNAME | m247mh2s2dpwpt4djrg2ynzodmsiadyw._domainkey.steward.narrativenode-labs.cloud | m247mh2s2dpwpt4djrg2ynzodmsiadyw.dkim.amazonses.com |
| MX | steward.narrativenode-labs.cloud | 10 inbound-smtp.us-east-1.amazonaws.com |
| TXT | _dmarc.steward.narrativenode-labs.cloud | v=DMARC1; p=none; |

AWS SES reported identity SUCCESS, DKIM SUCCESS, and sending verification enabled on 2026-09-10. The MX record resolves publicly to the AWS receiving endpoint. This verifies DNS configuration; it does not by itself verify a real message round trip. SES account sandbox limits and application dry-run delivery remain separate controls.

The subdomain DMARC record was added during vendor mailbox setup after a received
test message reported `dmarc=none`. `p=none` publishes a monitoring policy; aligned
DKIM signatures provide DMARC authentication. No aggregate report recipient is set.
