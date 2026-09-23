# Dedicated Preview Rendering Capacity

## Purpose

Xarta can reserve an optional Render process pool for synchronous Preview requests. This
is a Kubernetes capacity bulkhead, not a second rendering implementation or capability.

Both pools run:

- the same global Xarta image, tag, or digest;
- the same `render=latest` and `render=v1` service command;
- the same Render API and Python implementation;
- the same template-engine ConfigMap;
- the same document-type dependency;
- the same security context, probes, resource defaults, and scheduling constraints.

Only Preview's internal `RENDER_SERVICE_ENDPOINT` changes.

## Configuration

The feature is disabled by default:

```yaml
services:
  render:
    enabled: true
    replicas: 1
    templateRevision: null
    previewPool:
      enabled: false
      replicas: 1
```

Enable dedicated capacity:

```yaml
services:
  preview:
    enabled: true

  render:
    enabled: true
    replicas: 4
    templateRevision: "templates-892c4bf8"
    previewPool:
      enabled: true
      replicas: 2
```

The chart rejects a Preview Render pool when either Preview or Render is disabled, and
rejects a pool with fewer than one replica.

There is deliberately no Preview-specific image override. Every Render pool in one Helm
release executes one Xarta build.

## Resulting Topology

Disabled:

```text
Preview  ─┐
Generate ─┼─> <release>-service-render
Others   ─┘
```

Enabled:

```text
Preview ─────────────> <release>-service-render-preview

Generate / Others ───> <release>-service-render
```

The dedicated Service is internal-only. No Render Preview Ingress is created. The Preview
API remains the caller-facing boundary.

There is no automatic fallback from `render-preview` to the default Render Service. A
fallback would destroy the isolation guarantee exactly when the default pool is
saturated. An unhealthy Preview pool therefore produces an observable Preview failure.

## Kubernetes Resources

The normal resources remain:

```text
<release>-service-render       Deployment
<release>-service-render       Service
```

Enabling the feature adds:

```text
<release>-service-render-preview       Deployment
<release>-service-render-preview       Service
```

The existing normal Deployment selector remains unchanged:

```yaml
matchLabels:
  app: <release>-service-render
```

Classification labels are added to Deployment metadata and pod templates without
changing immutable selectors.

Default pool:

```yaml
app.kubernetes.io/name: xarta
app.kubernetes.io/component: render
xarta.peinser.com/render-pool: default
```

Preview pool:

```yaml
app.kubernetes.io/name: xarta
app.kubernetes.io/component: render
xarta.peinser.com/render-pool: preview
```

Useful queries:

```bash
kubectl get deployments,pods \
  -l app.kubernetes.io/component=render

kubectl get deployments,pods \
  -l app.kubernetes.io/component=render,xarta.peinser.com/render-pool=preview
```

These labels support template-GitOps discovery, logs, metrics, diagnostics, and emergency
operations. They are classification metadata, not Service selectors.

## Template Configuration And Revision

Both Deployments mount the ConfigMap named:

```text
<release>-service-render
```

Both use the checksum generated from the same
`templates/config-maps/template-engines.yaml` source. A template-engine configuration
change therefore rolls both pools consistently.

When `services.render.templateRevision` is set, both pod templates receive:

```yaml
xarta.peinser.com/template-revision: "<revision>"
```

Changing this value creates a normal declarative Kubernetes rollout of both pools. It is
useful for GitOps correlation and control-plane rollout intent.

The annotation does not imply that Xarta Render pods intrinsically cache all template
content. Xarta Render delegates rendering to configured template-engine services. Whether
a template publication requires any pod restart depends on how those downstream engines
load templates.

## Isolation Guarantee

This feature provides:

```text
Xarta Render process isolation        yes
Xarta Render replica isolation        yes
Kubernetes pod capacity isolation     yes
Preview-to-Render routing isolation   yes
```

It does not by itself provide:

```text
Jinja runtime capacity isolation      not necessarily
Scriptura capacity isolation          not necessarily
other template-engine isolation       not necessarily
```

Both Xarta Render pools initially call the same template-engine endpoints from
`services.render.templateEngines`. If the downstream engine is the bottleneck, additional
Render pods cannot guarantee Preview latency. Separate template-engine capacity is a
different architectural change.

## Rollout And Scaling

An Xarta image update rolls both pools because both use the global image reference.

A `templateRevision` update rolls both pools.

Changing only:

```yaml
services:
  render:
    previewPool:
      replicas: 3
```

changes only the Preview Render Deployment's desired replica count.

Disabling the feature routes Preview back to the normal Render Service and removes the
dedicated Deployment and Service during Helm reconciliation.

Independent resources or autoscaling for the Preview pool are intentionally deferred.
The chart currently has only global resource defaults and no active HPA template. Adding
a general workload-pool framework would be disproportionate to this bulkhead feature.

## Operational Verification

### Routing Smoke Test

With the feature disabled:

1. Submit a Preview request with a unique correlation ID.
2. Confirm the request reaches pods labelled `render-pool=default`.

With the feature enabled:

1. Submit a Preview request with a unique correlation ID.
2. Confirm it reaches only pods labelled `render-pool=preview`.
3. Submit a normal Generate request.
4. Confirm it reaches only pods labelled `render-pool=default`.

The application code has no pool-specific logging behavior. Use normal request correlation
IDs and Kubernetes pod metadata in the logging platform.

### Capacity Test

1. Sustain enough asynchronous generation traffic to occupy the default Render pool.
2. Repeatedly submit Preview requests.
3. Verify Preview requests continue reaching only the Preview Render Service and pods.
4. Verify normal generation never reaches the Preview pool.
5. Observe Preview progress and errors.
6. Separately monitor downstream template-engine saturation before claiming end-to-end
   latency isolation.

### Failure Test

Scale or disrupt the Preview Render Deployment while the feature is enabled. Preview
requests should fail according to normal timeout/unavailability behavior and must not
fall back to the default pool. Generate requests should continue using the default pool.

## Monitoring

Group Render metrics and logs by:

```text
app.kubernetes.io/component=render
xarta.peinser.com/render-pool=default|preview
```

Monitor at minimum:

- ready and desired replicas by pool;
- request count, latency, timeout, and error rate by pool;
- CPU, memory, throttling, and restart rate by pool;
- Preview API upstream failures;
- default Render queue or concurrency saturation;
- downstream template-engine request latency and saturation;
- image identity and template revision across both pools.

## Automated Verification

`make helm-verify` runs `k8s/helm/tests/preview-render-pool.sh`. The test renders the chart
for disabled and enabled topologies, custom tag, digest, template revision, replica count,
and invalid combinations. It protects Preview and Generate routing, image equality,
labels, command identity, ConfigMap/checksum sharing, and Service selectors.
