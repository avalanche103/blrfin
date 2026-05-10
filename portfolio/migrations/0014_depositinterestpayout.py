from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('portfolio', '0013_depositratechange'),
    ]

    operations = [
        migrations.CreateModel(
            name='DepositInterestPayout',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Создано')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='Обновлено')),
                ('payout_date', models.DateField(verbose_name='Дата выплаты')),
                ('interest_amount', models.DecimalField(decimal_places=2, max_digits=18, verbose_name='Сумма процентов')),
                ('notes', models.TextField(blank=True, verbose_name='Комментарий')),
                ('asset', models.ForeignKey(on_delete=models.deletion.PROTECT, related_name='interest_payouts', to='portfolio.asset', verbose_name='Депозит')),
                ('cash_transaction', models.OneToOneField(blank=True, null=True, on_delete=models.SET_NULL, related_name='deposit_interest_payout_record', to='portfolio.transaction', verbose_name='Денежная операция')),
            ],
            options={
                'verbose_name': 'выплата процентов по депозиту',
                'verbose_name_plural': 'выплаты процентов по депозитам',
                'ordering': ['payout_date', 'id'],
            },
        ),
        migrations.AddConstraint(
            model_name='depositinterestpayout',
            constraint=models.UniqueConstraint(fields=('asset', 'payout_date'), name='unique_deposit_interest_payout'),
        ),
    ]