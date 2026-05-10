from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('portfolio', '0014_depositinterestpayout'),
    ]

    operations = [
        migrations.AlterField(
            model_name='fxrate',
            name='effective_date',
            field=models.DateField(verbose_name='Дата курса'),
        ),
        migrations.RemoveConstraint(
            model_name='fxrate',
            name='unique_fx_pair',
        ),
        migrations.AddConstraint(
            model_name='fxrate',
            constraint=models.UniqueConstraint(fields=('from_currency', 'to_currency', 'effective_date'), name='unique_fx_pair_on_date'),
        ),
    ]