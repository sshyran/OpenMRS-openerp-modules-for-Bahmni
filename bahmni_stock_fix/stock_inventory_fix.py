import time

from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
from osv import fields, osv
from tools.translate import _
from openerp import netsvc
from openerp.tools import DEFAULT_SERVER_DATE_FORMAT, DEFAULT_SERVER_DATETIME_FORMAT, DATETIME_FORMATS_MAP, \
    float_compare

import itertools
from itertools import izip_longest
import logging

_logger = logging.getLogger(__name__)


def grouper(iterable, n, fillvalue=None):
    args = [iter(iterable)] * n
    return izip_longest(*args, fillvalue=fillvalue)


class stock_inventory_fix(osv.osv_memory):
    _name = "stock.inventory.fix"
    _inherit = "stock.inventory"
    _table = 'stock_inventory'

    def _inventory_line_hook(self, cr, uid, inventory_line, move_vals):
     """ Creates a stock move from an inventory line
     @param inventory_line:
     @param move_vals:
     @return:
     """
     return self.pool.get('stock.move').create(cr, uid, move_vals)

    def _get_quantity(self, cr, product_id, prodlot_id, location_id, unit_id):
        prodlot_criteria = "and sm.prodlot_id = %s" % prodlot_id if prodlot_id else "and sm.prodlot_id is null"
        cr.execute('''select sum(qty * product_uom.factor) as qty
          from (
               select sm.location_id, sm.product_id, sm.prodlot_id, -sum(sm.product_qty /uo.factor) as qty
               from stock_move as sm
                 left join stock_production_lot spl on (spl.id = sm.prodlot_id)
                 left join stock_location sl on (sl.id = sm.location_id)
                 left join product_uom uo on (uo.id=sm.product_uom)
               where state in ('done', 'confirmed', 'assigned', 'waiting')
               and sm.product_id = %%s
               %s
               and sm.location_id = %%s
               group by sm.location_id, sm.product_id, sm.product_uom, sm.prodlot_id

               union all

               select sm.location_dest_id as location_id, sm.product_id, sm.prodlot_id, sum(sm.product_qty /uo.factor) as qty
               from stock_move as sm
                 left join stock_production_lot spl on (spl.id = sm.prodlot_id)
                 left join stock_location sl on (sl.id = sm.location_dest_id)
                 left join product_uom uo on (uo.id=sm.product_uom)
               where sm.state in ('done', 'confirmed', 'assigned', 'waiting')
               and sm.product_id = %%s
               %s
               and sm.location_dest_id = %%s
               group by sm.location_dest_id, sm.product_id, sm.product_uom, sm.prodlot_id
             ) as report
              left join product_uom on (product_uom.id=%%s)
            group by location_id, product_id, prodlot_id, product_uom.id;''' % (prodlot_criteria, prodlot_criteria),
                   tuple([product_id, location_id, product_id, location_id, unit_id]))
        return cr.fetchone()

    def _get_all_quantity(self, cr, product_id, location_id):
        cr.execute('''select * from (select report.location_id, report.product_id, report.prodlot_id, sum(qty * product_uom.factor) as qty, product_uom.id as unit_id
            from (
                   select sm.location_id, sm.product_id, sm.prodlot_id, -sum(sm.product_qty /uo.factor) as qty
                   from stock_move as sm
                     left join stock_production_lot spl on (spl.id = sm.prodlot_id)
                     left join stock_location sl on (sl.id = sm.location_id)
                     left join product_uom uo on (uo.id=sm.product_uom)
                   where state in ('done', 'confirmed', 'assigned', 'waiting')
                   and sm.product_id = %s
                   and sm.location_id = %s
                   group by sm.location_id, sm.product_id, sm.product_uom, sm.prodlot_id

                   union all

                   select sm.location_dest_id as location_id, sm.product_id, sm.prodlot_id, sum(sm.product_qty /uo.factor) as qty
                   from stock_move as sm
                     left join stock_production_lot spl on (spl.id = sm.prodlot_id)
                     left join stock_location sl on (sl.id = sm.location_dest_id)
                     left join product_uom uo on (uo.id=sm.product_uom)
                   where sm.state in ('done', 'confirmed', 'assigned', 'waiting')
                   and sm.product_id = %s
                   and sm.location_dest_id = %s
                   group by sm.location_dest_id, sm.product_id, sm.product_uom, sm.prodlot_id
                 ) as report
              left join product_product on (product_product.id=report.product_id)
              left join product_template on (product_template.id=product_product.product_tmpl_id)
              left join product_uom on (product_uom.id=product_template.uom_id)
            group by location_id, product_id, prodlot_id, product_uom.id) temp
            where qty != 0;''',
                   tuple([product_id, location_id, product_id, location_id]))
        return cr.dictfetchall()

    def action_fix_inventory(self, cr, uid, ids, context=None):
        """ Confirm the inventory and writes its finished date
        @return: True
        """
        if context is None:
            context = {}
        # to perform the correct inventory corrections we need analyze stock location by
        # location, never recursively, so we use a special context

        for inv in self.browse(cr, uid, ids, context=context):
            move_ids = []
            for line in inv.inventory_line_id:
                pid = line.product_id.id
                move_prodlot_ids = []
                for row in self._get_all_quantity(cr, pid, line.location_id.id):
                    amount = self._get_quantity(cr, pid, row.get('prodlot_id'), line.location_id.id,row.get('unit_id'))
                    change = None
                    if amount:
                        change = 0 - amount[0]
                    if change:
                        location_id = line.product_id.property_stock_inventory.id
                        value = {
                            'name': _('INV:') + (line.inventory_id.name or ''),
                            'product_id': line.product_id.id,
                            'product_uom': row.get('unit_id'),
                            'prodlot_id':  row.get('prodlot_id'),
                            'date': inv.date,
                        }

                        if change > 0:
                            value.update( {
                                'product_qty': change,
                                'location_id': location_id,
                                'location_dest_id': line.location_id.id,
                            })
                        else:
                            value.update( {
                                'product_qty': -change,
                                'location_id': line.location_id.id,
                                'location_dest_id': location_id,
                            })

                        move_prodlot_ids.append(self._inventory_line_hook(cr, uid, line, value))
                self.write(cr, uid, [inv.id], {'state': 'confirm', 'move_prodlot_ids': [(6, 0, move_ids)]})
                self.pool.get('stock.move').action_confirm(cr, uid, move_prodlot_ids, context=context)

            for line in inv.inventory_line_id:
                pid = line.product_id.id
                amount = self._get_quantity(cr, pid, line.prod_lot_id.id, line.location_id.id, line.product_uom.id)
                if amount:
                  change = line.product_qty - amount[0]
                else:
                 change = line.product_qty
                lot_id = line.prod_lot_id.id
                if change:
                    location_id = line.product_id.property_stock_inventory.id
                    value = {
                        'name': _('INV:') + (line.inventory_id.name or ''),
                        'product_id': line.product_id.id,
                        'product_uom': line.product_uom.id,
                        'prodlot_id': lot_id,
                        'date': inv.date,
                    }

                    if change > 0:
                        value.update( {
                            'product_qty': change,
                            'location_id': location_id,
                            'location_dest_id': line.location_id.id,
                        })
                    else:
                        value.update( {
                            'product_qty': -change,
                            'location_id': line.location_id.id,
                            'location_dest_id': location_id,
                        })
                    move_ids.append(self._inventory_line_hook(cr, uid, line, value))
            self.write(cr, uid, [inv.id], {'state': 'confirm', 'move_ids': [(6, 0, move_ids)]})
            self.pool.get('stock.move').action_confirm(cr, uid, move_ids, context=context)
        return True


stock_inventory_fix()
