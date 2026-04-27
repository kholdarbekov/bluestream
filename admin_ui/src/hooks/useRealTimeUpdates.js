import React, { useEffect, useCallback, useRef } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { message } from 'antd';

// WebSocket connection manager
class WebSocketManager {
  constructor() {
    this.socket = null;
    this.reconnectAttempts = 0;
    this.maxReconnectAttempts = 5;
    this.reconnectDelay = 1000;
    this.subscribers = new Map();
    this.isConnecting = false;
  }

  connect(url, token) {
    if (this.socket && this.socket.readyState === WebSocket.OPEN) {
      return;
    }

    if (this.isConnecting) {
      return;
    }

    this.isConnecting = true;

    try {
      this.socket = new WebSocket(`${url}?token=${token}`);

      this.socket.onopen = () => {
        console.log('WebSocket connected');
        this.isConnecting = false;
        this.reconnectAttempts = 0;
        this.notifySubscribers('connect', { type: 'connected' });
      };

      this.socket.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          this.notifySubscribers('message', data);
        } catch (error) {
          console.error('WebSocket message parse error:', error);
        }
      };

      this.socket.onclose = (event) => {
        console.log('WebSocket disconnected:', event.code, event.reason);
        this.isConnecting = false;
        this.notifySubscribers('disconnect', { code: event.code, reason: event.reason });

        // Attempt reconnection
        if (this.reconnectAttempts < this.maxReconnectAttempts) {
          this.scheduleReconnect(url, token);
        }
      };

      this.socket.onerror = (error) => {
        console.error('WebSocket error:', error);
        this.isConnecting = false;
        this.notifySubscribers('error', { error });
      };
    } catch (error) {
      console.error('WebSocket connection error:', error);
      this.isConnecting = false;
    }
  }

  scheduleReconnect(url, token) {
    this.reconnectAttempts++;
    const delay = this.reconnectDelay * Math.pow(2, this.reconnectAttempts - 1);

    setTimeout(() => {
      console.log(`Attempting to reconnect (${this.reconnectAttempts}/${this.maxReconnectAttempts})...`);
      this.connect(url, token);
    }, delay);
  }

  subscribe(id, callback) {
    this.subscribers.set(id, callback);
  }

  unsubscribe(id) {
    this.subscribers.delete(id);
  }

  notifySubscribers(type, data) {
    this.subscribers.forEach(callback => {
      try {
        callback(type, data);
      } catch (error) {
        console.error('Subscriber callback error:', error);
      }
    });
  }

  send(data) {
    if (this.socket && this.socket.readyState === WebSocket.OPEN) {
      this.socket.send(JSON.stringify(data));
    } else {
      console.warn('WebSocket not connected, cannot send data');
    }
  }

  disconnect() {
    if (this.socket) {
      this.socket.close();
      this.socket = null;
    }
    this.subscribers.clear();
  }
}

// Singleton instance
const wsManager = new WebSocketManager();

// Hook for real-time updates
export const useRealTimeUpdates = (options = {}) => {
  const {
    enabled = true,
    autoConnect = true,
    queries = [], // Array of query keys to invalidate
    onUpdate = null,
    onConnect = null,
    onDisconnect = null,
    onError = null
  } = options;

  const queryClient = useQueryClient();
  const subscriberIdRef = useRef(null);

  const handleMessage = useCallback((type, data) => {
    switch (type) {
      case 'connect':
        if (onConnect) onConnect(data);
        break;

      case 'disconnect':
        if (onDisconnect) onDisconnect(data);
        break;

      case 'error':
        if (onError) onError(data);
        break;

      case 'message':
        // eslint-disable-next-line no-use-before-define
        handleRealTimeUpdate(data);
        if (onUpdate) onUpdate(data);
        break;

      default:
        break;
    }
  }, [onConnect, onDisconnect, onError, onUpdate]);

  const handleRealTimeUpdate = useCallback((data) => {
    const { type, payload } = data;

    switch (type) {
      case 'order_status_updated':
        queryClient.invalidateQueries({
          queryKey: ['orders'],
        });
        queryClient.invalidateQueries({
          queryKey: ['dashboard'],
        });
        message.info(`Order ${payload.order_number} status updated to ${payload.status}`);
        break;

      case 'new_order_created':
        queryClient.invalidateQueries({
          queryKey: ['orders'],
        });
        queryClient.invalidateQueries({
          queryKey: ['dashboard'],
        });
        message.success(`New order ${payload.order_number} received`);
        break;

      case 'delivery_status_updated':
        queryClient.invalidateQueries({
          queryKey: ['deliveries'],
        });
        queryClient.invalidateQueries({
          queryKey: ['dashboard'],
        });
        if (payload.status === 'delivered') {
          message.success(`Delivery ${payload.delivery_id} completed`);
        }
        break;

      case 'product_stock_low':
        queryClient.invalidateQueries({
          queryKey: ['products'],
        });
        message.warning(`Low stock alert: ${payload.product_name} (${payload.stock_quantity} remaining)`);
        break;

      case 'product_out_of_stock':
        queryClient.invalidateQueries({
          queryKey: ['products'],
        });
        message.error(`Out of stock: ${payload.product_name}`);
        break;

      case 'user_registered':
        queryClient.invalidateQueries({
          queryKey: ['users'],
        });
        queryClient.invalidateQueries({
          queryKey: ['dashboard'],
        });
        break;

      case 'loyalty_points_updated':
        queryClient.invalidateQueries({
          queryKey: ['loyalty-members'],
        });
        queryClient.invalidateQueries({
          queryKey: ['analytics-loyalty'],
        });
        if (payload.points_added > 0) {
          message.info(`${payload.customer_name} earned ${payload.points_added} points`);
        }
        break;

      case 'notification_sent':
        queryClient.invalidateQueries({
          queryKey: ['notification-campaigns'],
        });
        break;

      case 'dashboard_metrics_updated':
        queryClient.invalidateQueries({
          queryKey: ['dashboard'],
        });
        queryClient.invalidateQueries({
          queryKey: ['analytics'],
        });
        break;

      case 'system_alert':
        if (payload.level === 'error') {
          message.error(payload.message);
        } else if (payload.level === 'warning') {
          message.warning(payload.message);
        } else {
          message.info(payload.message);
        }
        break;

      default:
        // Invalidate specific queries if provided
        queries.forEach(queryKey => {
          queryClient.invalidateQueries({
            queryKey,
          });
        });
        break;
    }
  }, [queryClient, queries]);

  const connect = useCallback(() => {
    const token = localStorage.getItem('token');
    const wsUrl = process.env.REACT_APP_WEBSOCKET_URL || 'ws://localhost:5000/ws';

    if (token && enabled) {
      wsManager.connect(wsUrl, token);
    }
  }, [enabled]);

  const disconnect = useCallback(() => {
    if (subscriberIdRef.current) {
      wsManager.unsubscribe(subscriberIdRef.current);
    }
    wsManager.disconnect();
  }, []);

  const sendMessage = useCallback((message) => {
    wsManager.send(message);
  }, []);

  useEffect(() => {
    if (!enabled) return;

    // Generate unique subscriber ID
    subscriberIdRef.current = `subscriber_${Date.now()}_${Math.random()}`;

    // Subscribe to WebSocket events
    wsManager.subscribe(subscriberIdRef.current, handleMessage);

    // Auto connect if enabled
    if (autoConnect) {
      connect();
    }

    // Cleanup on unmount
    return () => {
      if (subscriberIdRef.current) {
        wsManager.unsubscribe(subscriberIdRef.current);
      }
    };
  }, [enabled, autoConnect, connect, handleMessage]);

  return {
    connect,
    disconnect,
    sendMessage,
    isConnected: wsManager.socket?.readyState === WebSocket.OPEN
  };
};

// Hook for polling-based updates (fallback)
export const usePollingUpdates = (options = {}) => {
  const {
    enabled = true,
    interval = 30000, // 30 seconds
    queries = [],
    onUpdate = null
  } = options;

  const queryClient = useQueryClient();
  const intervalRef = useRef(null);

  const startPolling = useCallback(() => {
    if (intervalRef.current) {
      clearInterval(intervalRef.current);
    }

    intervalRef.current = setInterval(() => {
      queries.forEach(queryKey => {
        queryClient.invalidateQueries({
          queryKey,
        });
      });

      if (onUpdate) {
        onUpdate({ type: 'polling_update', timestamp: new Date() });
      }
    }, interval);
  }, [queryClient, queries, interval, onUpdate]);

  const stopPolling = useCallback(() => {
    if (intervalRef.current) {
      clearInterval(intervalRef.current);
      intervalRef.current = null;
    }
  }, []);

  useEffect(() => {
    if (enabled) {
      startPolling();
    }

    return () => {
      stopPolling();
    };
  }, [enabled, startPolling, stopPolling]);

  return {
    startPolling,
    stopPolling,
    isPolling: intervalRef.current !== null
  };
};

// Combined hook that uses WebSocket with polling fallback
export const useRealTimeWithFallback = (options = {}) => {
  const {
    enableWebSocket = true,
    enablePolling = true,
    pollingInterval = 60000, // 1 minute fallback
    ...wsOptions
  } = options;

  const [isWebSocketConnected, setIsWebSocketConnected] = React.useState(false);

  const wsHook = useRealTimeUpdates({
    ...wsOptions,
    enabled: enableWebSocket,
    onConnect: (data) => {
      setIsWebSocketConnected(true);
      wsOptions.onConnect?.(data);
    },
    onDisconnect: (data) => {
      setIsWebSocketConnected(false);
      wsOptions.onDisconnect?.(data);
    },
    onError: (data) => {
      setIsWebSocketConnected(false);
      wsOptions.onError?.(data);
    }
  });

  const pollingHook = usePollingUpdates({
    ...wsOptions,
    enabled: enablePolling && !isWebSocketConnected,
    interval: pollingInterval
  });

  return {
    ...wsHook,
    isPolling: pollingHook.isPolling,
    connectionType: isWebSocketConnected ? 'websocket' : (pollingHook.isPolling ? 'polling' : 'none')
  };
};

export default useRealTimeUpdates;
