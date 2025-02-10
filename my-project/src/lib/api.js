import axios from 'axios';

const API_BASE_URL = 'http://127.0.0.1:8000';

export const createApi = () => {
  return axios.create({
    baseURL: API_BASE_URL
  });
};