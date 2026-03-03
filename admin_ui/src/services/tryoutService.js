import api from './api';

const unwrap = (response) => response?.data?.data || response?.data || {};

class TryoutService {
  async getTryouts(params = {}) {
    const response = await api.get('/admin/tryouts', { params });
    return unwrap(response);
  }

  async getTryout(tryoutId) {
    const response = await api.get(`/admin/tryouts/${tryoutId}`);
    return unwrap(response)?.tryout || null;
  }

  async createTryout(payload) {
    const response = await api.post('/admin/tryouts', payload);
    return unwrap(response)?.tryout || null;
  }

  async updateTryout(tryoutId, payload) {
    const response = await api.put(`/admin/tryouts/${tryoutId}`, payload);
    return unwrap(response)?.tryout || null;
  }

  async convertTryout(tryoutId) {
    const response = await api.post(`/admin/tryouts/${tryoutId}/convert`, {});
    const data = unwrap(response);
    return {
      tryout: data?.tryout || null,
      conversion: data?.conversion || null,
    };
  }

  async createTask(tryoutId, payload) {
    const response = await api.post(`/admin/tryouts/${tryoutId}/tasks`, payload);
    return unwrap(response)?.task || null;
  }

  async assignTask(taskId, assignedDriverUserId) {
    const response = await api.put(`/admin/tryout-tasks/${taskId}/assign`, {
      assigned_driver_user_id: assignedDriverUserId
    });
    return unwrap(response)?.task || null;
  }

  async completeTask(taskId, payload = {}) {
    const response = await api.post(`/admin/tryout-tasks/${taskId}/complete`, payload);
    return unwrap(response)?.tryout || null;
  }

  async adjustBottles(tryoutId, payload) {
    const response = await api.post(`/admin/tryouts/${tryoutId}/adjust-bottles`, payload);
    return unwrap(response)?.tryout || null;
  }

  async exportTryouts(params = {}) {
    const response = await api.get('/admin/tryouts/export', {
      params,
      responseType: 'blob'
    });
    return response.data;
  }
}

export default new TryoutService();
