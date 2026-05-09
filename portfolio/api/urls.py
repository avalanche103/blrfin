from django.urls import path

from . import views

app_name = 'portfolio_api'

urlpatterns = [
    path('accounts/', views.accounts_api, name='accounts'),
    path('transfers/', views.transfers_api, name='transfers'),
    path('portfolio/', views.portfolio_api, name='portfolio'),
    path('assets/', views.assets_api, name='assets'),
    path('transactions/', views.transactions_api, name='transactions'),
    path('prices/', views.prices_api, name='prices'),
]