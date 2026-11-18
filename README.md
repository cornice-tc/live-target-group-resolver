# live-target-group-resolver

CloudFormation custom resource (AWS Lambda, handler `live_target_group_resolver.handler`) that returns the live target group of an ECS Express Mode blue/green listener. It waits for the service's deployment to settle first, and enables `routing.http.drop_invalid_header_fields` on the load balancer.

`cfnresponse.py` is AWS's module, vendored unmodified.

## Build

```bash
bash scripts/build_zip.sh   # produces dist/live-target-group-resolver.zip
```

## Contributions

Published for review. Contributions, issues, and pull requests are not accepted.
