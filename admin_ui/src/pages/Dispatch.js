import React, { useMemo, useState } from 'react';
import {
  Alert, Badge, Button, Card, Checkbox, Col, DatePicker, Empty, List, Row, Space, Spin, Tag, Typography,
} from 'antd';
import {
  useMutation, useQueries, useQuery, useQueryClient,
} from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import dayjs from 'dayjs';
import toast from 'react-hot-toast';
import OperationsMap from '../components/OperationsMap';
import DriverRoutePanel from '../components/dispatch/DriverRoutePanel';
import PoolPanel from '../components/dispatch/PoolPanel';
import {
  buildSavePayload, clampPins, isDirty, routeStopSignature, togglePin,
} from '../components/dispatch/dispatchLogic';
import adminService from '../services/adminService';

const { Text, Title } = Typography;

const Dispatch = () => {
  const { t } = useTranslation('delivery');
  const queryClient = useQueryClient();

  const [day, setDay] = useState(dayjs());
  const [layers, setLayers] = useState({ customers: false, orders: true, drivers: true });
  const [selectedDriverId, setSelectedDriverId] = useState(null);
  // Set by clicking an unassigned order marker on the map (OperationsMap's
  // `onSelectStop`); read by PoolPanel to highlight the matching row.
  // Selection only — no map-based drag-to-assign.
  const [selectedPoolStopId, setSelectedPoolStopId] = useState(null);
  // driverId -> { ids: number[], pinned: {} } while unsaved.
  const [drafts, setDrafts] = useState({});
  // The map and the stop lists no longer share a row, so a stop picked in a
  // panel has to be able to move the map — otherwise stacking them would have
  // traded a layout bug for a workflow one. Holds `[lat, lng]` of the last
  // stop clicked in a panel; OperationsMap pans there without changing zoom.
  const [focusPoint, setFocusPoint] = useState(null);
  // Collapsing the map turns the page into a pure worklist for bulk
  // resequencing, where the map is just scroll distance between panels.
  const [mapCollapsed, setMapCollapsed] = useState(false);
  const mapHeight = mapCollapsed ? 220 : 600;

  const focusStop = (stop) => {
    setSelectedPoolStopId(stop.delivery_id);
    if (stop.lat != null && stop.lng != null) setFocusPoint([stop.lat, stop.lng]);
  };

  // driverId -> { currentIds } while that driver's route has an unresolved
  // 409. Keyed the same way as `drafts`: a save on driver B's route must not
  // clear driver A's still-unresolved conflict banner, or the admin loses
  // the only signal that A's draft needs reconciling against a route that
  // moved under them.
  const [conflicts, setConflicts] = useState({});

  const anyDirtyDraft = Object.keys(drafts).length > 0;

  const { data, isLoading } = useQuery({
    queryKey: ['dispatchSnapshot', day.format('YYYY-MM-DD')],
    queryFn: () => adminService.getDispatchSnapshot({ date: day.format('YYYY-MM-DD') }),
    // A refetch mid-drag would clobber the draft the admin is building, so
    // polling stops while anything is unsaved and resumes on save or discard.
    refetchInterval: anyDirtyDraft ? false : 30000,
  });
  const snapshot = data?.data || {};
  const routes = snapshot.routes || [];
  const drivers = snapshot.drivers || [];

  const driverName = useMemo(
    () => Object.fromEntries(drivers.map((d) => [d.driver_id, d.full_name])),
    [drivers],
  );

  // Real road geometry is a separate, per-driver endpoint (see
  // admin_dispatch.py's dispatch_route_geometry). Fetched for EVERY visible
  // route, not just a selected one — the product decision is always real
  // road geometry, and the backend caches on a sequence hash with a 15-minute
  // TTL, so panning and the 30s snapshot poll are free; only a real sequence
  // change re-fetches.
  //
  // The key carries the route's STOP SIGNATURE, not just the driver id, which
  // is what makes "only a real sequence change re-fetches" true rather than
  // merely intended. `invalidate()` below covers changes THIS page makes; the
  // signature covers every change it does not — a reassignment from the
  // Delivery page, a bulk action, a driver claiming in the staff bot, another
  // admin's browser. Those arrive through the 30s snapshot poll, and a
  // driver-only key meant the panels updated underneath a polyline that kept
  // its original shape for as long as the tab stayed open. New stops => new
  // key => fetch; identical stops => cache hit, so the poll stays free.
  // Still no `refetchInterval`: with the signature in the key there is nothing
  // for a timer to discover that the snapshot has not already reported.
  const geometryQueries = useQueries({
    queries: routes.map((route) => ({
      queryKey: [
        'dispatchRouteGeometry', day.format('YYYY-MM-DD'), route.driver_id, routeStopSignature(route.stops),
      ],
      queryFn: () => adminService.getDispatchRouteGeometry(
        route.driver_id, { date: day.format('YYYY-MM-DD') },
      ),
    })),
  });
  const geometry = useMemo(
    () => Object.fromEntries(
      routes
        // `.at(index)`, not `geometryQueries[index]`: a computed member
        // expression trips `security/detect-object-injection` even though
        // `index` only ever comes from this same `.map()`, never caller data.
        .map((route, index) => [route.driver_id, geometryQueries.at(index)?.data?.data])
        .filter(([, payload]) => payload != null),
    ),
    [routes, geometryQueries],
  );

  // Both the snapshot and the currently-fetched road geometry describe the
  // same driver sequence, so any mutation that can change that sequence
  // (save, reoptimize, assign, unassign) must invalidate both — otherwise
  // the map keeps drawing a real, solid polyline for a route nobody is
  // driving anymore.
  //
  // Invalidated on the bare 'dispatchRouteGeometry' prefix, which matches every
  // (date, driverId, signature) variant, rather than the route that changed:
  // react-query prefix matching is element-wise, so this stays correct as the
  // key grows, and a move affects at least two routes anyway. It is the
  // belt to the signature's braces — the signature catches changes made
  // elsewhere, this catches our own without waiting for the next poll.
  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['dispatchSnapshot'] });
    queryClient.invalidateQueries({ queryKey: ['dispatchRouteGeometry'] });
  };

  const saveMutation = useMutation({
    mutationFn: ({ driverId, payload }) => adminService.setDispatchStops(driverId, payload),
    onSuccess: (_res, { driverId }) => {
      setDrafts((prev) => {
        const next = { ...prev };
        // driverId comes from the mutation's own variables, not user input.
        // eslint-disable-next-line security/detect-object-injection
        delete next[driverId];
        return next;
      });
      setConflicts((prev) => {
        const next = { ...prev };
        // eslint-disable-next-line security/detect-object-injection
        delete next[driverId];
        return next;
      });
      toast.success(t('ui.dispatch.saved', 'Route saved'));
      invalidate();
    },
    onError: (error, { driverId }) => {
      if (error?.response?.status === 409) {
        setConflicts((prev) => ({
          ...prev,
          [driverId]: { currentIds: error.response.data?.data?.current_delivery_ids || [] },
        }));
        return;
      }
      toast.error(error?.response?.data?.message || t('ui.dispatch.save_failed', 'Could not save the route'));
    },
  });

  const reoptimizeMutation = useMutation({
    mutationFn: (driverId) => adminService.reoptimizeDispatchRoute(driverId),
    onSuccess: (_res, driverId) => {
      setDrafts((prev) => {
        const next = { ...prev };
        // driverId comes from the mutation's own variables, not user input.
        // eslint-disable-next-line security/detect-object-injection
        delete next[driverId];
        return next;
      });
      invalidate();
    },
    onError: (error) => toast.error(error?.response?.data?.message || t('ui.dispatch.reoptimize_failed', 'Re-optimisation failed')),
  });

  const assignMutation = useMutation({
    mutationFn: ({ deliveryId, body }) => adminService.assignDispatchStop(deliveryId, body),
    onSuccess: invalidate,
    // The server refuses moves for real reasons (COD-blocked driver, stop no
    // longer claimable). Show its message rather than a generic failure.
    onError: (error) => toast.error(error?.response?.data?.message || t('ui.dispatch.move_failed', 'Could not move this stop')),
  });

  const unassignMutation = useMutation({
    mutationFn: ({ deliveryId, body }) => adminService.unassignDispatchStop(deliveryId, body),
    onSuccess: invalidate,
    onError: (error) => toast.error(error?.response?.data?.message || t('ui.dispatch.pool_failed', 'Could not pool this stop')),
  });

  const serverStateFor = (route) => ({
    ids: route.stops.map((s) => s.delivery_id),
    pinned: route.stops.reduce((acc, s) => (s.pinned ? { ...acc, [String(s.delivery_id)]: s.position } : acc), {}),
  });

  const stateFor = (route) => drafts[route.driver_id] || serverStateFor(route);

  const updateDraft = (route, next) => {
    const server = serverStateFor(route);
    setDrafts((prev) => {
      const copy = { ...prev };
      if (isDirty(next.ids, next.pinned, server.ids, server.pinned)) {
        copy[route.driver_id] = next;
      } else {
        delete copy[route.driver_id];
      }
      return copy;
    });
  };

  if (isLoading) {
    return <div style={{ padding: 48, textAlign: 'center' }}><Spin /></div>;
  }

  return (
    <div>
      <Space wrap style={{ marginBottom: 12 }}>
        <Title level={4} style={{ margin: 0 }}>{t('ui.dispatch.title', 'Dispatch')}</Title>
        <DatePicker value={day} onChange={(d) => d && setDay(d)} allowClear={false} />
        <Checkbox checked={layers.orders} onChange={(e) => setLayers({ ...layers, orders: e.target.checked })}>
          {t('ui.dispatch.layer_orders', 'Active orders')}
        </Checkbox>
        <Checkbox checked={layers.drivers} onChange={(e) => setLayers({ ...layers, drivers: e.target.checked })}>
          {t('ui.dispatch.layer_drivers', 'Drivers & routes')}
        </Checkbox>
        {anyDirtyDraft && (
          <Text type="warning" data-testid="polling-paused">
            {t('ui.dispatch.polling_paused', 'Auto-refresh paused — unsaved changes')}
          </Text>
        )}
        <Badge count={(snapshot.unmapped || []).length} showZero>
          <Text data-testid="unmapped-count">{(snapshot.unmapped || []).length}</Text>
        </Badge>
        <Text type="secondary">{t('ui.dispatch.unmapped', 'orders not on the map')}</Text>
        <Button size="small" data-testid="map-collapse" onClick={() => setMapCollapsed((v) => !v)}>
          {mapCollapsed
            ? t('ui.dispatch.expand_map', 'Expand map')
            : t('ui.dispatch.collapse_map', 'Collapse map')}
        </Button>
      </Space>

      {/* The map is a full-width band ABOVE the panels, not a grid neighbour
          competing with them for one row. Two reasons, in order of weight:
          the map is the thing an admin reads the city from and it was
          previously boxed into ~62% of the page; and a panel that shares a
          row with a Leaflet map has no way to win a paint-order argument with
          it (Leaflet's panes sit at z-index 200-1000 and nothing here creates
          a stacking context), so any overflow lands underneath the map. Side
          by side, that was a permanent hazard; stacked, it cannot happen.

          The cost is that the map and a stop list are no longer both on
          screen at once. `onSelectStop` keeps them connected: picking a stop
          on the map highlights its row, and picking a row focuses the map. */}
      <div style={{ marginBottom: 12 }}>
        <OperationsMap
          height={mapHeight}
          customers={[]}
          orders={snapshot.orders || []}
          drivers={drivers}
          routes={routes}
          geometry={geometry}
          visibleLayers={layers}
          selectedDriverId={selectedDriverId}
          onSelectDriver={setSelectedDriverId}
          onSelectStop={(stop) => setSelectedPoolStopId(stop.delivery_id)}
          focusPoint={focusPoint}
        />
      </div>

      <Row gutter={12}>
        <Col xs={24} lg={8}>
          <Space direction="vertical" size={8} style={{ width: '100%' }}>
            <PoolPanel
              stops={snapshot.pool || []}
              drivers={drivers}
              assigning={assignMutation.isPending}
              onAssign={(deliveryId, driverId) => assignMutation.mutate({
                deliveryId, body: { driver_id: driverId },
              })}
              selectedDeliveryId={selectedPoolStopId}
              onFocusStop={focusStop}
            />
          </Space>
        </Col>

        <Col xs={24} lg={16}>
          <Space direction="vertical" size={8} style={{ width: '100%' }}>
            {routes.length === 0 && <Empty description={t('ui.dispatch.no_routes', 'No planned routes today')} />}
            {routes.map((route) => {
              const state = stateFor(route);
              const server = serverStateFor(route);
              const orderedStops = state.ids
                .map((id, index) => {
                  const stop = route.stops.find((s) => s.delivery_id === id);
                  return stop ? { ...stop, position: index } : null;
                })
                .filter(Boolean);
              const routeConflict = conflicts[route.driver_id];
              return (
                <React.Fragment key={route.driver_id}>
                  {routeConflict && (
                    <Alert
                      data-testid="route-conflict"
                      type="warning"
                      showIcon
                      message={t('ui.dispatch.conflict_title', 'This route changed while you were editing it')}
                      description={t('ui.dispatch.conflict_body', 'Discard your draft and re-apply it on the refreshed route.')}
                      action={(
                        <Button size="small" onClick={() => {
                          setDrafts((prev) => {
                            const next = { ...prev };
                            delete next[route.driver_id];
                            return next;
                          });
                          setConflicts((prev) => {
                            const next = { ...prev };
                            delete next[route.driver_id];
                            return next;
                          });
                          invalidate();
                        }}
                        >
                          {t('ui.dispatch.conflict_reload', 'Reload route')}
                        </Button>
                      )}
                    />
                  )}
                  <DriverRoutePanel
                    route={{ ...route, driver_name: driverName[route.driver_id] }}
                    drivers={drivers}
                    stops={orderedStops}
                    pinned={state.pinned}
                    dirty={Boolean(drafts[route.driver_id])}
                    saving={saveMutation.isPending}
                    // Straight off the geometry payload, including the id list
                    // that says which stop each leg arrives at. Not zipped
                    // against `orderedStops` here: the backend drops stops it
                    // could not geocode before measuring, so position is not a
                    // valid join key (see DriverRoutePanel's legByDeliveryId).
                    legs={geometry[route.driver_id]?.legs ?? null}
                    legDeliveryIds={geometry[route.driver_id]?.leg_delivery_ids ?? null}
                    onFocusStop={focusStop}
                    onReorder={(ids) => updateDraft(route, { ids, pinned: clampPins(state.pinned, ids) })}
                    onTogglePin={(deliveryId) => updateDraft(route, {
                      ids: state.ids,
                      pinned: togglePin(state.pinned, state.ids, deliveryId),
                    })}
                    onMove={(deliveryId, toDriverId) => assignMutation.mutate({
                      deliveryId, body: { driver_id: toDriverId },
                    })}
                    onPool={(deliveryId) => unassignMutation.mutate({ deliveryId, body: { reason: null } })}
                    onSave={() => saveMutation.mutate({
                      driverId: route.driver_id,
                      payload: buildSavePayload(state, server.ids),
                    })}
                    onDiscard={() => updateDraft(route, server)}
                    onReoptimize={() => reoptimizeMutation.mutate(route.driver_id)}
                  />
                </React.Fragment>
              );
            })}

            {(snapshot.unmapped || []).length > 0 && (
              <Card size="small" title={t('ui.dispatch.unmapped_title', 'Not on the map')}>
                <List
                  size="small"
                  dataSource={snapshot.unmapped}
                  renderItem={(item) => {
                    // `DispatchService` emits exactly two reasons today (see
                    // business_app/services/dispatch_service.py):
                    // `not_scheduled` takes precedence over `no_coordinates`
                    // when both would apply. An explicit two-way match — not
                    // a ternary that defaults anything unrecognised to one of
                    // the two known labels, and not an object keyed by the
                    // server string (which would also trip
                    // `security/detect-object-injection` on a computed member
                    // lookup) — so a future third reason (or a malformed
                    // payload) renders as neutrally "unknown" instead of
                    // being confidently mislabelled. A silently-wrong label
                    // here is exactly bug #5 this task fixed: a row shown
                    // under a heading that doesn't describe it.
                    let reasonLabel;
                    if (item.reason === 'no_coordinates') {
                      reasonLabel = t('ui.dispatch.reason_no_coordinates', 'No coordinates');
                    } else if (item.reason === 'not_scheduled') {
                      reasonLabel = t('ui.dispatch.reason_not_scheduled', 'Not scheduled');
                    } else {
                      reasonLabel = t('ui.dispatch.reason_unknown', 'Unknown reason');
                    }
                    return (
                      <List.Item>
                        <Space size={8} wrap>
                          <Text>{item.order_number} · {item.customer_name}</Text>
                          <Tag data-testid={`unmapped-reason-${item.order_id}`}>{reasonLabel}</Tag>
                        </Space>
                      </List.Item>
                    );
                  }}
                />
              </Card>
            )}
          </Space>
        </Col>
      </Row>
    </div>
  );
};

export default Dispatch;
