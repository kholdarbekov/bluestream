import React, { useMemo, useState } from 'react';
import {
  Alert, Badge, Button, Card, Checkbox, Col, DatePicker, Empty, List, Row, Space, Spin, Typography,
} from 'antd';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import dayjs from 'dayjs';
import toast from 'react-hot-toast';
import OperationsMap from '../components/OperationsMap';
import DriverRoutePanel from '../components/dispatch/DriverRoutePanel';
import PoolPanel from '../components/dispatch/PoolPanel';
import { buildSavePayload, clampPins, isDirty, togglePin } from '../components/dispatch/dispatchLogic';
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
  // admin_dispatch.py's dispatch_route_geometry / DispatchService's module
  // docstring: "geometry is polled per selected driver" precisely so the map
  // + 30s poll never has to pay for N routing calls at once). We only ever
  // resolve it for the currently-selected driver; every other route keeps
  // OperationsMap's honest dashed-straight-line fallback until picked.
  const { data: geometryData } = useQuery({
    queryKey: ['dispatchRouteGeometry', selectedDriverId],
    queryFn: () => adminService.getDispatchRouteGeometry(selectedDriverId),
    enabled: selectedDriverId != null,
  });
  const geometry = useMemo(
    () => (selectedDriverId != null && geometryData?.data ? { [selectedDriverId]: geometryData.data } : {}),
    [selectedDriverId, geometryData],
  );

  // Both the snapshot and the currently-fetched road geometry describe the
  // same driver sequence, so any mutation that can change that sequence
  // (save, reoptimize, assign, unassign) must invalidate both — otherwise
  // the map keeps drawing a real, solid polyline for a route nobody is
  // driving anymore. Invalidated on the shared 'dispatchRouteGeometry'
  // prefix (matches every driverId-keyed variant) rather than the one
  // currently selected, since a move/pool can affect a route other than the
  // selected one. No polling here: the backend caches geometry on the
  // sequence hash, so a post-mutation refetch is cheap and a timer would
  // just be noise on an endpoint that rarely changes.
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
        <Text type="secondary">{t('ui.dispatch.unmapped', 'orders without coordinates')}</Text>
      </Space>

      <Row gutter={12}>
        <Col xs={24} lg={9}>
          <Space direction="vertical" size={8} style={{ width: '100%' }}>
            <PoolPanel
              stops={snapshot.pool || []}
              drivers={drivers}
              assigning={assignMutation.isPending}
              onAssign={(deliveryId, driverId) => assignMutation.mutate({
                deliveryId, body: { driver_id: driverId },
              })}
              selectedDeliveryId={selectedPoolStopId}
            />

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
              <Card size="small" title={t('ui.dispatch.unmapped_title', 'Not on the map (no coordinates)')}>
                <List
                  size="small"
                  dataSource={snapshot.unmapped}
                  renderItem={(item) => (
                    <List.Item>
                      <Text>{item.order_number} · {item.customer_name}</Text>
                    </List.Item>
                  )}
                />
              </Card>
            )}
          </Space>
        </Col>

        <Col xs={24} lg={15}>
          <OperationsMap
            height={720}
            customers={[]}
            orders={snapshot.orders || []}
            drivers={drivers}
            routes={routes}
            geometry={geometry}
            visibleLayers={layers}
            selectedDriverId={selectedDriverId}
            onSelectDriver={setSelectedDriverId}
            onSelectStop={(stop) => setSelectedPoolStopId(stop.delivery_id)}
          />
        </Col>
      </Row>
    </div>
  );
};

export default Dispatch;
